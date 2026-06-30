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

# ── Recipe (one-click Format) fee ─────────────────────────────────────────────
# Flat orchestration fee added ON TOP of the Σ per-step meters (image keyframes +
# video clips + TTS + music) for a one-click recipe like Product Ad. It is NOT a new
# pricing primitive — recipe_product_ad.estimate() sums the existing per-step meters
# and appends this fee; that same total is the umbrella hold (badge == charge).
RECIPE_FEE = int(os.getenv("RECIPE_FEE_CREDITS", "25"))

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
    # NEW image-page registry models (atlabs picker) — tiered by COGS. The fail-closed 'pro' default was
    # INVERTING the gate: nano-banana-pro-ultra ($0.15) reachable at Pro while cheap models ($0.003) were
    # over-locked. Re-tier via pricing.json model_min_tier.image (no code change) if the product call differs.
    "flux-schnell": "free", "gpt-image-1-mini": "free", "ernie-image-turbo": "free", "z-image-turbo": "free",
    "flux-dev": "starter", "grok-imagine": "starter", "qwen-image": "starter", "seedream-5": "starter",
    "mai-image-2-5": "starter",
    "imagen-4": "pro", "recraft-v3": "pro",
    "youchuan-v8": "enterprise", "nano-banana-pro-ultra": "enterprise",
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
    s = str(size or "").lower()
    if s and ("2160" in s or "3840" in s or "4k" in s or "uhd" in s):
        return "enterprise"        # 4K is resolution-locked to Studio, any base model (accepts "4K" label too)
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
# request omits a resolution. NOTE: Sora is resolution-invariant (stays on the flat
# map). Kling (per-second: no-audio base 84 + 4K 315) and Wan (placeholder = Kling
# no-audio 84) live in the GLOBAL config's video_usd_per_sec_by_res, NOT in this code
# default — this stays empty so Indonesia keeps flat per-second pricing. See the
# global config note for the deferred Kling audio/turbo variants.
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


# ── Fail-loud guard: the GLOBAL deployment MUST load the global config ──────────
# When BILLING_MODE=subscription (the global product), the config that actually got
# LOADED must carry the global markers. If it doesn't, the loader silently fell back
# to the Indonesia config (config/pricing.json) → $0.01 economics + empty by-res map
# → ~80% silent margin leak (sell $0.002, charge calibrated $0.01) + Kling/Wan/Veo
# flat. Refuse to start instead of bleeding margin in silence. Indonesia
# (BILLING_MODE != subscription) skips this entirely. Presence-only checks (NO
# hardcoded $0.002) so re-pricing never trips the guard — these three keys are
# absent / Indonesia-default in config/pricing.json, so they cleanly detect a
# fallback. Requires BILLING_MODE=subscription to be set on THIS service.
def _assert_global_config_loaded(pricing: dict) -> None:
    if os.getenv("BILLING_MODE") != "subscription":
        return
    missing = []
    sp = pricing.get("subscription_plans")
    if not (isinstance(sp, dict) and sp):
        missing.append("subscription_plans (absent/empty)")
    byres = pricing.get("video_usd_per_sec_by_res")
    if not (isinstance(byres, dict) and byres):
        missing.append("video_usd_per_sec_by_res (absent/empty)")
    cuv = pricing.get("credit_usd_value")
    if cuv is None or float(cuv) == 0.01:
        missing.append("credit_usd_value (absent or ==0.01 Indonesia default)")
    if missing:
        raise RuntimeError(
            "GLOBAL CONFIG NOT LOADED — PRICING_CONFIG_JSON stale/unset, fallback ke "
            "ekonomi Indonesia. Refusing start. Missing/invalid global config keys: "
            + "; ".join(missing)
            + ". Fix: set PRICING_CONFIG_JSON (or PRICING_CONFIG_PATH) on this service "
            "to the current config/pricing.global.example.json."
        )


_assert_global_config_loaded(_PRICING)


# ── Monthly credit allowance per plan (config-driven, per-key fallback) ────────
TIER_MONTHLY_CREDITS: dict[str, int] = {
    **_DEFAULT_TIER_MONTHLY_CREDITS,
    **(_PRICING.get("tier_monthly_credits") or {}),
}

# ── GLOBAL revenue recognition: derive per-plan list price + allowance from the
# subscription_plans block so credit_sale_price_idr() recognises the REAL plan price.
# Without this, the global deployment's plans (starter/plus/pro/ultra) are absent from
# TIER_PRICE_IDR (Indonesia defaults only have starter/pro/enterprise) → plus/ultra fell
# back to the blended CREDIT_SALE_PRICE_IDR (Rp248 ≈ 7× the real Rp36/cr) and pro/starter
# used Indonesia IDR list prices ÷ global allowance → revenue mis-stated every direction.
# subscription_plans prices are in USD (Dodo) → convert at KURS_IDR_USD so revenue_idr stays
# the IDR presentation of cash collected. Only fires when subscription_plans is present
# (the global product); Indonesia (config/pricing.json, no subscription_plans) is untouched.
# NOTE (functional-currency, roadmap): for USD-native books the accounting engine should read
# credits × CREDIT_USD_VALUE (or cost_usd) directly; revenue_idr here is the IDR view at KURS.
_SUB_PLANS = _PRICING.get("subscription_plans") or {}
if isinstance(_SUB_PLANS, dict) and _SUB_PLANS:
    for _pk, _pv in _SUB_PLANS.items():
        if not isinstance(_pv, dict):
            continue
        _pcr = _pv.get("credits")
        _pusd = _pv.get("price_usd")
        if _pcr:                                   # allowance drives both the grant and the /cr price
            TIER_MONTHLY_CREDITS[_pk] = int(_pcr)
        if _pusd and _pcr:                         # paid plan → IDR list price = USD price × KURS
            TIER_PRICE_IDR[_pk] = round(float(_pusd) * KURS_IDR_USD, 2)

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


# Round the SELL price (credits) UP to the next multiple of IMG_CREDIT_ROUNDUP (5):
# 9→10, 13→15, 17→20, 137→140, 171→175. Cleaner pricing + a sliver more margin.
# SCOPED TO op=="image" only (in credit_cost) so it never bumps the live whiteboard
# render fee (3cr/sec), TTS, chat, or video pricing. Set IMG_CREDIT_ROUNDUP=1 to disable.
IMG_CREDIT_ROUNDUP = int(os.getenv("IMG_CREDIT_ROUNDUP", "5"))
def _roundup_credits(c: int, step: int = IMG_CREDIT_ROUNDUP) -> int:
    if c <= 0 or step <= 1:
        return c
    return ((c + step - 1) // step) * step

def credit_cost(operation: str, model: str, units: Union[int, float, dict]) -> int:
    """Credits to charge for `operation` on `model` consuming `units`.
    This is the function the metering middleware calls. See operation_usd for the
    meaning of `units` per operation. Image/video carry a harga-jual markup; image
    sell price is additionally rounded UP to the next multiple of IMG_CREDIT_ROUNDUP."""
    usd = operation_usd(operation, model, units)
    credits = usd_to_credits(usd * _op_markup(operation, model, units, usd))
    if (operation or "").lower() == "image":
        credits = _roundup_credits(credits)
    return credits


# New Image-page sell rule (Rino 2026-06-28): price off the FIRST-PARTY price (the MOST
# EXPENSIVE provider in the failover chain — e.g. Vertex/OpenAI/BFL) × a thin markup, NOT
# the cheapest aggregator × 3×. The real margin then comes from sourcing a cheaper aggregator
# at runtime (failover never loses money — worst case = first-party = the markup itself).
# Banner badge == this == what's debited.
IMG_SELL_MARKUP    = float(os.getenv("IMG_SELL_MARKUP",    "1.10"))  # arbitrage-floor markup (first-party × this)
IMG_MIN_MARGIN     = float(os.getenv("IMG_MIN_MARGIN",     "0.50"))  # real-margin floor (sell >= cogs/(1-floor))
IMG_PREMIUM_MARKUP = float(os.getenv("IMG_PREMIUM_MARKUP", "1.30"))  # fixed markup for margin-floor-EXEMPT ops (creative upscale)
def image_credits_for_usd(firstparty_usd: float, cogs_usd: float = None, markup: float = None) -> int:
    """Credits for ONE image = first-party (most-expensive-source) USD priced by a DOUBLE FLOOR,
    converted at CREDIT_USD_VALUE, rounded UP to IMG_CREDIT_ROUNDUP. Single source of truth so the
    picker badge == the debit. Sell = max(arbitrage floor, margin floor):
      • arbitrage floor = first-party × IMG_SELL_MARKUP (1.10) — keeps the fat margin on models
        where the cheapest aggregator (cogs_usd) is far below first-party (e.g. GPT-Image 93%).
      • margin floor    = cogs_usd / (1 - IMG_MIN_MARGIN) (= 2×cogs at 50%) — guarantees ≥50% real
        margin even on near-zero-arbitrage models (Recraft/Imagen, where cogs≈first-party).
    Round-up only RAISES margin, so the floor holds. cogs_usd omitted → arbitrage floor only.
    `markup` set → fixed first-party×markup, BYPASSING the margin floor (premium-op exemption,
    e.g. creative upscale via IMG_PREMIUM_MARKUP). e.g. Nano Banana $0.039×1.10→25cr (62%);
    Recraft $0.04: max($0.044, $0.08)=$0.08→40cr (50%). (Legacy /generate-image 3× path untouched.)"""
    if not firstparty_usd or firstparty_usd <= 0:
        return 0
    if markup is not None:
        return _roundup_credits(usd_to_credits(firstparty_usd * markup))
    cr = _roundup_credits(usd_to_credits(firstparty_usd * IMG_SELL_MARKUP))   # arbitrage floor
    if cogs_usd and cogs_usd > 0 and IMG_MIN_MARGIN < 1.0:
        cr_floor = _roundup_credits(usd_to_credits(cogs_usd / (1.0 - IMG_MIN_MARGIN)))  # margin floor
        cr = max(cr, cr_floor)
    return cr


# ══════════════════════════════════════════════════════════════════════════════
# Image BATCH pricing (async Google Batch API — the "Batch" tool only)
# ══════════════════════════════════════════════════════════════════════════════
# Google's Batch API genuinely costs ~50% of the online price, so we sell batch at
# 50% of the regular sell price, rounded UP to IMG_CREDIT_ROUNDUP (5). These are
# LOCKED values (Rino 2026-06-29) — NOT recomputed from cogs at runtime, because a
# batch is priced before any image exists. Margin stays strongly positive on real
# native-Google batch COGS (0.5×official): nano-banana 15cr, -2 25cr, -pro 40cr.
# nano-banana-pro-ultra is EXCLUDED (no first-party Google batch model behind it).
_IMAGE_BATCH_CREDITS = {
    "nano-banana":     15,
    "nano-banana-2":   25,
    "nano-banana-pro": 40,
}
# Native-Google model id per auth path. Vertex (OAuth) uses the GA names; the
# Developer API (API key) carries a `-preview` suffix on the Gemini-3 image models.
# Both are FIRST-PARTY Google — no aggregator. (Source of truth: image_registry.json
# `vertex` chain + the laozhang_api _IMG_MODEL Developer map.)
_IMAGE_BATCH_VERTEX = {
    "nano-banana":     "gemini-2.5-flash-image",
    "nano-banana-2":   "gemini-3.1-flash-image",
    "nano-banana-pro": "gemini-3-pro-image",
}
_IMAGE_BATCH_DEVELOPER = {
    "nano-banana":     "gemini-2.5-flash-image",
    "nano-banana-2":   "gemini-3.1-flash-image-preview",
    "nano-banana-pro": "gemini-3-pro-image-preview",
}


def is_batch_eligible(model: str) -> bool:
    """True iff `model` can run on the async Google Batch path (priced at 50%)."""
    return (model or "") in _IMAGE_BATCH_CREDITS


def image_batch_credits(model: str) -> int:
    """Batch sell price (credits) for ONE image of `model`. Raises KeyError for an
    ineligible model so a caller can't silently charge the wrong amount — the submit
    endpoint guards with is_batch_eligible() and 400s first."""
    return _IMAGE_BATCH_CREDITS[model]


def image_batch_vertex_model(model: str) -> str:
    """Google model id for the Vertex (OAuth) batch path."""
    return _IMAGE_BATCH_VERTEX[model]


def image_batch_developer_model(model: str) -> str:
    """Google model id for the Developer (API-key) batch path (Gemini-3 = `-preview`)."""
    return _IMAGE_BATCH_DEVELOPER[model]


# Real batch COGS (USD) = 0.5 × Google's official online price (image_registry official_usd).
# Passed to commit_credits.cost_usd so usage_logs/margin reports reflect true batch cost,
# not the online single-image estimate. nano-banana 0.039→0.0195, -2 0.067→0.0335, -pro 0.134→0.067.
_IMAGE_BATCH_COGS_USD = {
    "nano-banana":     0.0195,
    "nano-banana-2":   0.0335,
    "nano-banana-pro": 0.0670,
}


def image_batch_cogs_usd(model: str, count: int = 1) -> float:
    """True batch COGS (USD) for `count` delivered images of `model`."""
    return _IMAGE_BATCH_COGS_USD.get(model, 0.0) * max(0, int(count))
# Video-page sell rule v2 (per-model caps + audio). Double floor [max(arbitrage, margin)] on the
# PER-SECOND USD, multiplied by an audio multiplier and the (integer) duration, then the round-up-to-5
# is applied ONCE to the FULL-CLIP credit total (NOT per-second). This matches the v2 BUILD CONTRACT:
#   perSecUSD = max(official × markup, 2 × cogs) × audio_mult
#   credits   = ceil5( perSecUSD × seconds / CREDIT_USD_VALUE )
# So the frontend MUST display the TOTAL (ceil5 of the whole clip), not perSec×seconds — and the
# backend debits the identical TOTAL (badge == charge). The video multi-provider backend
# (video_providers.dispatch) fails over cheapest→source, so pricing off the first-party / op-chain-MAX
# guarantees failover only ever RAISES margin (never negative). Distinct markup default from image:
# VIDEO_SELL_MARKUP (1.10). audio_mult comes from the model's audio_on_mult when audio is on (or the
# model emits native always-on audio); 1.0 otherwise. The margin floor uses the contract's literal
# 2×cogs form (== cogs/(1-0.50) at the shared 50% floor).
VIDEO_SELL_MARKUP = float(os.getenv("VIDEO_SELL_MARKUP", "1.10"))   # first-party per-sec × this (arbitrage floor)
def video_credits_for_usd(official_usd: float, cogs_usd: float = None,
                          seconds: float = _VIDEO_DEFAULT_SECONDS, markup: float = None,
                          audio_mult: float = 1.0) -> int:
    """SELL credits for a video clip (v2 per-model-caps + audio formula):

        perSecUSD = max( official_usd × markup,  2 × cogs_usd ) × audio_mult
        credits   = ceil5( perSecUSD × seconds / CREDIT_USD_VALUE )

      • arbitrage floor = official_usd × markup (VIDEO_SELL_MARKUP, default 1.10) — keeps margin on
        models where the cheapest aggregator (cogs) is far below the first-party/official price.
      • margin floor    = 2 × cogs_usd (= cogs/(1 - IMG_MIN_MARGIN) at the shared 50% floor) —
        guarantees ≥50% real margin even on near-zero-arbitrage models.
      • audio_mult      = the model's audio_on_mult when audio is on / native-always, else 1.0.

    ceil5 (round UP to nearest IMG_CREDIT_ROUNDUP=5) is applied ONCE to the FULL-CLIP credit total
    (NOT per-second), so the displayed badge == the metered charge exactly. `markup` set → fixed
    official×markup, bypassing the margin floor (premium-op exemption). `official_usd` falsey → 0.
    Round-up only raises margin, so the floor always holds. Single source of truth shared by the picker
    badge and the metered debit — video_providers.credits_for() calls through here at the chosen
    duration (and seconds=1 for a per-second display figure)."""
    if not official_usd or official_usd <= 0:
        return 0
    if markup is None:
        markup = VIDEO_SELL_MARKUP
    per_sec_usd = official_usd * markup                                  # arbitrage floor (per second, USD)
    if markup == VIDEO_SELL_MARKUP and cogs_usd and cogs_usd > 0:
        per_sec_usd = max(per_sec_usd, 2.0 * cogs_usd)                   # margin floor (2×cogs)
    try:
        am = float(audio_mult) if audio_mult else 1.0
    except (TypeError, ValueError):
        am = 1.0
    if am <= 0:
        am = 1.0
    per_sec_usd *= am
    try:
        secs = int(seconds or _VIDEO_DEFAULT_SECONDS)
    except (TypeError, ValueError):
        secs = int(_VIDEO_DEFAULT_SECONDS)
    if secs <= 0:
        secs = int(_VIDEO_DEFAULT_SECONDS)
    # ceil5 ONCE on the full-clip credit total: usd_to_credits ceils to whole credits, then round up to 5.
    return _roundup_credits(usd_to_credits(per_sec_usd * secs))


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

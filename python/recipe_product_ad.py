"""
recipe_product_ad.py — the Product Ad recipe: planner + DAG sequencer + estimate.
Per PRODUCT-AD-IMPL-CONTRACT §2 (recipe_product_ad.py). Full design:
~/docs/wimba-product-ad-recipe-spec.md.

This module turns 1–4 product photos into a finished, multi-aspect product ad video. It owns the
ORCHESTRATION; every paid primitive is reused unchanged:

    plan()                — ONE capped LLM call (metered chat, tagged video_job) → beats + script.
    run_product_ad_job()  — the full DAG sequencer for ALL THREE styles (showcase / in_scene / ugc):
                              auto-caption → bg_remove → per-variant plan → hero beats (locked
                              keyframes via recipe_fidelity.make_keyframes → animated by
                              video_providers.dispatch) → b-roll beats (text_to_video) → TTS →
                              recipe_assembly.assemble() per aspect → _persist_asset.
    estimate()            — Σ per-step credits (image/video/tts meters) + RECIPE_FEE; drives BOTH the
                              /estimate receipt AND the umbrella hold amount.

Billing discipline (contract §2): ONE umbrella hold_credits(estimate.total, op_id); every render runs
IN-PROCESS; a final commit_credits(committed_total, op_id) settles the actual (commit < hold refunds
the slack automatically); every ledger line carries video_job=<job_id> so /credits/history rolls the
whole ad up into ONE row; ANY failure refunds the entire umbrella hold. The op_id is the server-minted
umbrella id (recipe-<uuid>) — hold/commit/refund are idempotent on it.

FIDELITY (contract hard rule): the product surface is ALWAYS the user's real pixels (deterministic
paste-back in recipe_fidelity); the AI only ever renders the SCENE around it. Text / logo / CTA are
ALWAYS ffmpeg overlays (recipe_assembly), NEVER generative.

Style → model defaults (cheapest-first, resolved from the LIVE video registry):
    showcase  hero = image_to_video  (seedance-2-mini → veo-3-1-fast)   camera-static
    in_scene  hero = reference_to_video (seedance-2-mini → seedance-2)   cutouts as refs
    ugc       hero = image_to_video on an AVATAR model (omnihuman-avatar → kling-avatar) + lip_sync
    b-roll    = text_to_video  (seedance-2-mini → veo-3-1-fast)          product absent / tiny

Imports are LAZY inside functions (this module is imported BY laozhang_api, whose helpers we reuse —
a top-level import would be circular). Pillow is a transitive dep via recipe_fidelity (NOT yet in
requirements.txt — see PENDING in the build report).
"""
from __future__ import annotations

import os
import json
import time
import base64
import logging
import asyncio
from typing import Optional, Any

log = logging.getLogger("recipe_product_ad")

# ── tunables ──────────────────────────────────────────────────────────────────
# Flat recipe convenience fee (the recipe's own value-add; per-step COGS is billed separately).
# Mirrors credit_catalog.RECIPE_FEE if that constant exists; else read the env directly so this
# module is correct even before credit_catalog.py is edited (additive-scope safety).
def _recipe_fee() -> int:
    try:
        import credit_catalog as _cat
        v = getattr(_cat, "RECIPE_FEE", None)
        if v is not None:
            return int(v)
    except Exception:
        pass
    return int(os.getenv("RECIPE_FEE_CREDITS", "25"))


# Music bed flat charge (v1: no licensed library wired → a flat credit if requested, or skip cleanly).
RECIPE_MUSIC_CREDITS = int(os.getenv("RECIPE_MUSIC_CREDITS", "5"))
# Per-hero-beat clip length cap (contract: ≤5s, camera-on-static so the product never morphs).
RECIPE_HERO_MAX_SECONDS = int(os.getenv("RECIPE_HERO_MAX_SECONDS", "5"))
# TTS model for the voiceover — Gemini 3.1 Flash audio (Vertex OAuth; in the backend GEMINI_TTS_CHAIN).
# Failover within _synth_vo cycles to the rest of GEMINI_TTS_CHAIN (2.5-flash, 2.5-pro) on the same voices.
RECIPE_TTS_MODEL = os.getenv("RECIPE_TTS_MODEL", "gemini-3.1-flash-tts-preview")
# Music bed generator — instrumental commercial background via fal (cassetteai = no lyrics, fast, cheap).
RECIPE_MUSIC_MODEL = os.getenv("RECIPE_MUSIC_MODEL", "cassetteai/music-generator")
RECIPE_MUSIC_POLL_MAX = float(os.getenv("RECIPE_MUSIC_POLL_MAX", "240"))
# Planner LLM (short, latency-sensitive, capped) — a fast reliable chat model.
RECIPE_PLAN_MODEL = os.getenv("RECIPE_PLAN_MODEL", "claude-sonnet-4-6")
# Hard cap on beats regardless of seconds (contract: cap beats at 6).
RECIPE_MAX_BEATS = int(os.getenv("RECIPE_MAX_BEATS", "6"))
# Keyframe image resolution basis for the per-keyframe composite price (an image meter, per frame).
# scene_plate + harmonize are two image ops per keyframe; we price both.
RECIPE_KEYFRAME_IMG_OPS = 2  # scene_plate (create_raster) + harmonize (edit)


# ── style → model + feature defaults (cheapest-first; resolved against the live registry) ──────
# Each entry: ordered model ladder for the HERO animate, the dispatch FEATURE, and whether the
# style needs an avatar/lip-sync pass. b-roll always text_to_video on the cheap ladder.
_HERO_MODELS = {
    "showcase": ["seedance-2-mini", "veo-3-1-fast"],
    "in_scene": ["seedance-2-mini", "seedance-2"],
    "ugc":      ["omnihuman-avatar", "kling-avatar"],
}
_HERO_FEATURE = {
    "showcase": "image_to_video",       # FLF2V / i2v, camera-static
    "in_scene": "reference_to_video",   # cutouts as references
    "ugc":      "image_to_video",       # avatar frame → talking head
}
_BROLL_MODELS = ["seedance-2-mini", "veo-3-1-fast"]
_BROLL_FEATURE = "text_to_video"
# Keyframe scene plate / harmonize use the IMAGE backend; price off these image features.
_KF_SCENE_FEATURE = "create_raster"
_KF_HARMONIZE_FEATURE = "edit"
# image models used to PRICE the keyframe ops (the fidelity module's own ladders pick the live one;
# pricing off the cheapest-first head keeps badge == charge conservative-low without under-charging,
# since the margin floor in image_credits_for_usd holds regardless of which model serves).
_KF_SCENE_MODEL = "seedream-4-5"
_KF_HARMONIZE_MODEL = "nano-banana"
# Default per-clip render resolution for recipe clips (kept modest for cost; 720p is the sweet spot).
RECIPE_CLIP_RES = os.getenv("RECIPE_CLIP_RES", "720p")
# How many clips to render concurrently within ONE ad job. Clips are independent model calls; rendering
# them in parallel cuts wall-clock from sum(clips) to max(clips). Bounded so a long ad (many beats)
# doesn't hammer a single provider's concurrency limit. 3 covers the common 8–15s multi-shot ad.
_CLIP_CONCURRENCY = int(os.getenv("RECIPE_CLIP_CONCURRENCY", "3"))


# ══════════════════════════════ small helpers ══════════════════════════════════
def _first_live_model(candidates: list, feature: str) -> Optional[str]:
    """First model id from `candidates` that exists in the live video registry AND supports `feature`.
    Lets a registry trim or a missing avatar model degrade gracefully (next ladder entry) instead of
    dispatching a 404 slug. Returns None if none qualify."""
    import video_providers as _vp
    for mid in candidates:
        m = _vp._MODELS.get(mid)
        if m and feature in (m.get("features") or []):
            return mid
    return None


def _hero_resolved(style: str) -> tuple:
    """(model_id, feature) for the hero animate of a style — cheapest live model that supports the
    style's feature, falling back to image_to_video if the preferred feature isn't available on any
    candidate (e.g. reference_to_video trimmed → animate the keyframe via i2v instead)."""
    feat = _HERO_FEATURE.get(style, "image_to_video")
    mid = _first_live_model(_HERO_MODELS.get(style, _BROLL_MODELS), feat)
    if mid is None and feat != "image_to_video":
        # graceful: animate the locked keyframe as a still i2v if the ref/avatar path is unavailable.
        feat = "image_to_video"
        mid = _first_live_model(_HERO_MODELS.get(style, _BROLL_MODELS) + _BROLL_MODELS, feat)
    if mid is None:
        mid = _first_live_model(_BROLL_MODELS, "image_to_video")
        feat = "image_to_video"
    return mid, feat


def _broll_resolved() -> tuple:
    mid = _first_live_model(_BROLL_MODELS, _BROLL_FEATURE)
    return mid, _BROLL_FEATURE


def _norm_seconds(v) -> int:
    try:
        s = int(v)
    except (TypeError, ValueError):
        s = 8
    return s if s in (8, 15, 30) else (8 if s <= 8 else (15 if s <= 15 else 30))


def _shot_count_for(seconds: int, style: str = "showcase") -> tuple:
    """(n_hero, n_broll) for a total ad length. 8s → 1 hero; 15s → 2 hero + 1 broll;
    30s → 2 hero + 3 broll (≤ RECIPE_MAX_BEATS total). Contract: 8→1; 15→2-3; 30→4-5.
    ugc is special: ONE talking-head presenter carries the FULL voiceover and the rest are silent
    product cutaways under the continuous VO. A single hero keeps lip-sync aligned — multiple avatar
    clips each synced to the whole VO would drift once assembly re-muxes one global VO track."""
    if style == "ugc":
        if seconds <= 8:
            return 1, 0
        if seconds <= 15:
            return 1, 2
        return 1, 4
    if seconds <= 8:
        return 1, 0
    if seconds <= 15:
        return 2, 1
    return 2, 3


def _per_clip_seconds(total_seconds: int, n_clips: int) -> int:
    """Even split of the ad length across clips, clamped to a sane per-clip render length so the
    product never morphs over a long take (contract: hero ≤5s, camera-on-static)."""
    if n_clips <= 0:
        return RECIPE_HERO_MAX_SECONDS
    per = max(1, round(total_seconds / n_clips))
    return min(per, max(4, RECIPE_HERO_MAX_SECONDS))


def _aspects(input: dict) -> list:
    asp = input.get("aspects") or ["9:16"]
    out = [a for a in asp if a in ("9:16", "1:1", "16:9")]
    return out or ["9:16"]


def _variants(input: dict) -> int:
    try:
        v = int(input.get("variants") or 1)
    except (TypeError, ValueError):
        v = 1
    return min(3, max(1, v))


def _audio_on_for(model_id: str) -> bool:
    """Whether to price/render audio ON for a clip model: a 'toggle' model gets audio on (the ad has a
    soundtrack), a 'none' model never does, an 'always' model is native-on (priced via audio_on_mult)."""
    import video_providers as _vp
    kind = (_vp._MODELS.get(model_id) or {}).get("audio", "none")
    return kind != "none"


def _kf_each_credits() -> int:
    """Per-keyframe image COGS = scene_plate (create_raster) + harmonize (edit). This is the SINGLE
    source the estimate HOLDS and the run COMMITS, so the keyframe line is badge == charge. (Previously
    estimate held this but the run never committed it → keyframes were silently refunded as slack, a real
    margin leak; both sides now read this same number.)"""
    import image_providers as _ip
    return (int(_ip.credits_for(_KF_SCENE_FEATURE, _KF_SCENE_MODEL) or 0)
            + int(_ip.credits_for(_KF_HARMONIZE_FEATURE, _KF_HARMONIZE_MODEL) or 0))


# ══════════════════════════════ planner ════════════════════════════════════════
def _fallback_plan(input: dict, n_hero: int, n_broll: int, per_hero: int, per_broll: int) -> dict:
    """Deterministic plan when the LLM is unavailable / returns garbage — never blocks a render. Builds
    plain beats from the product description + style so the DAG always has a shot list."""
    style = (input.get("style") or "showcase").lower()
    desc = (input.get("product_desc") or "the product").strip()
    vibe = (input.get("vibe") or "minimal").lower()
    scene = (input.get("scene_prompt") or "").strip()
    beats = []
    for i in range(n_hero):
        beats.append({
            "role": "hero",
            "scene_prompt": scene or f"{desc}, {vibe} {style} hero shot",
            "motion": "slow push-in, camera static, product locked center",
            "seconds": per_hero,
        })
    for i in range(n_broll):
        beats.append({
            "role": "broll",
            "scene_prompt": f"{vibe} lifestyle b-roll for {desc}, product absent or tiny, supporting mood",
            "motion": "gentle ambient motion",
            "seconds": per_broll,
        })
    cta = (input.get("cta") or "").strip()
    script = ""
    if (input.get("voiceover") or {}).get("on"):
        script = (input.get("voiceover") or {}).get("script") or (
            f"Meet {desc}. {cta}" if cta else f"Meet {desc}.")
    return {"beats": beats[:RECIPE_MAX_BEATS], "script": script,
            "shot_count": min(RECIPE_MAX_BEATS, n_hero + n_broll)}


async def plan(input: dict) -> dict:
    """ONE capped LLM call → a strict shot plan. Returns:
        { beats: [{role:"hero"|"broll", scene_prompt, motion, seconds}], script: str, shot_count: int }

    seconds→shots: 8s→1 hero; 15s→2-3; 30s→4-5 (1-2 hero + b-roll). Beats are capped at
    RECIPE_MAX_BEATS (6). The chat call is METERED and tagged video_job (the umbrella op_id) so the
    planner's tokens roll into the ad's single history row. Best-effort: any failure → _fallback_plan
    (the DAG must never be blocked by the planner).

    `input` carries the server-injected billing context under _tenant_id / _user_id / _op_id when called
    from run_product_ad_job; a bare /estimate-style call (no context) simply skips the debit."""
    seconds = _norm_seconds(input.get("seconds"))
    style = (input.get("style") or "showcase").lower()
    n_hero, n_broll = _shot_count_for(seconds, style)
    per_hero = _per_clip_seconds(seconds, max(1, n_hero + n_broll))
    per_broll = per_hero
    vibe = (input.get("vibe") or "minimal").lower()
    desc = (input.get("product_desc") or "").strip()
    language = (input.get("language") or "English").strip()
    scene_prompt = (input.get("scene_prompt") or "").strip()
    vo = input.get("voiceover") or {}
    want_vo = bool(vo.get("on"))
    cta = (input.get("cta") or "").strip()

    tenant_id = input.get("_tenant_id")
    user_id = input.get("_user_id")
    op_id = input.get("_op_id")
    byok = bool(input.get("_byok"))

    prompt = (
        "You are a senior ad creative director planning a SHORT product ad video. Plan a tight shot "
        "list as STRICT JSON ONLY (no prose, no fences), this EXACT shape:\n"
        '{"beats":[{"role":"hero"|"broll","scene_prompt":"...","motion":"...","seconds":N}],'
        '"script":"...","shot_count":N}\n'
        "RULES:\n"
        f"- Total ad length is {seconds}s. Produce EXACTLY {n_hero} 'hero' beat(s) and {n_broll} "
        "'broll' beat(s), in shooting order (heroes first). NEVER exceed "
        f"{RECIPE_MAX_BEATS} beats total.\n"
        "- A HERO beat features the PRODUCT prominently; describe the SCENE/background only (the real "
        "product is composited in separately — do NOT describe redrawing the product). motion must be "
        "SUBTLE and camera-near-static (e.g. 'slow push-in', 'slow orbit ≤10°') so the product never "
        "warps. Each hero beat 'seconds' ≤ " + str(RECIPE_HERO_MAX_SECONDS) + ".\n"
        "- A BROLL beat is mood/lifestyle with the product ABSENT or tiny.\n"
        f"- style='{style}', vibe='{vibe}'." +
        (f" Scene direction: {scene_prompt}." if scene_prompt else "") +
        (f" Product: {desc}." if desc else "") + "\n"
        + ("- Write a spoken 'script' (the voiceover), in " + language +
           ", natural and persuasive, timed to ~" + str(seconds) + "s of speech" +
           (f", ending on the call-to-action: '{cta}'." if cta else ".") + "\n"
           if want_vo else "- Leave 'script' as an empty string (no voiceover).\n")
        + "Output ONLY the JSON object."
    )

    try:
        import laozhang_api as _api
        cl = _api.make_client(RECIPE_PLAN_MODEL)
        mt = min(1500, _api.MODEL_MAX_TOKENS.get(
            _api.MODELS.get(RECIPE_PLAN_MODEL, RECIPE_PLAN_MODEL), _api.DEFAULT_MAX_TOKENS))

        def _call(use_fmt: bool):
            kw = dict(model=RECIPE_PLAN_MODEL, messages=[{"role": "user", "content": prompt}],
                      temperature=0.6, max_tokens=mt)
            if use_fmt:
                kw["response_format"] = {"type": "json_object"}
            return cl.chat.completions.create(**kw)

        try:
            r = await asyncio.to_thread(lambda: _call(True))
        except Exception as _fmt:
            log.info("plan json_object rejected (%s); retrying plain", _fmt)
            r = await asyncio.to_thread(lambda: _call(False))

        raw = (r.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            import re as _re
            raw = _re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = _re.sub(r"\n?```\s*$", "", raw).strip()
        parsed = json.loads(raw)

        # NOTE: the planner's small chat COGS is intentionally NOT metered as a separate ledger line.
        # It is absorbed by the flat recipe fee (a generous catch-all > the planner+caption token cost),
        # which keeps a SINGLE charge mechanism (the umbrella commit) and exact badge == charge — a
        # second direct debit here used to be charged ON TOP of the displayed estimate.
        return _coerce_plan(parsed, input, n_hero, n_broll, per_hero, per_broll)
    except Exception as e:
        log.info("plan LLM failed (%s) → deterministic fallback", e)
        return _fallback_plan(input, n_hero, n_broll, per_hero, per_broll)


def _coerce_plan(parsed: Any, input: dict, n_hero: int, n_broll: int,
                 per_hero: int, per_broll: int) -> dict:
    """Validate + clamp the LLM plan into the strict schema (defensive: the model may over/under-produce
    beats, drop roles, or emit oversized seconds). Falls back to the deterministic plan if unusable."""
    if not isinstance(parsed, dict):
        return _fallback_plan(input, n_hero, n_broll, per_hero, per_broll)
    raw_beats = parsed.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        return _fallback_plan(input, n_hero, n_broll, per_hero, per_broll)
    heroes, brolls = [], []
    for b in raw_beats:
        if not isinstance(b, dict):
            continue
        role = "broll" if (b.get("role") or "").lower() == "broll" else "hero"
        sp = (b.get("scene_prompt") or "").strip()
        if not sp:
            continue
        try:
            secs = int(b.get("seconds") or (per_broll if role == "broll" else per_hero))
        except (TypeError, ValueError):
            secs = per_hero
        secs = min(max(1, secs), RECIPE_HERO_MAX_SECONDS)
        beat = {"role": role, "scene_prompt": sp,
                "motion": (b.get("motion") or "subtle motion, camera static").strip(), "seconds": secs}
        (heroes if role == "hero" else brolls).append(beat)
    # Enforce the requested shape (clamp counts; backfill heroes from the fallback if the model gave none).
    if not heroes:
        fb = _fallback_plan(input, n_hero, n_broll, per_hero, per_broll)
        heroes = [x for x in fb["beats"] if x["role"] == "hero"][:max(1, n_hero)]
    heroes = heroes[:max(1, n_hero)]
    brolls = brolls[:max(0, n_broll)]
    beats = (heroes + brolls)[:RECIPE_MAX_BEATS]
    script = parsed.get("script")
    if not isinstance(script, str):
        script = ""
    return {"beats": beats, "script": script.strip(), "shot_count": len(beats)}


# ══════════════════════════════ estimate ═══════════════════════════════════════
def estimate(input: dict, catalog: Any = None) -> dict:
    """Σ per-step credits for the WHOLE ad + RECIPE_FEE → {line_items:[{label,credits}], total}. This is
    the single source for BOTH the /estimate receipt AND the umbrella hold amount (badge == charge). All
    per-step prices come from the SAME meters the renders will use:
        keyframes  → image_providers.credits_for(create_raster/edit)   (per keyframe, per image op)
        clips      → video_providers.credits_for(feature, model, seconds, res, audio)
        VO         → credit_catalog.estimate_tts_credits(model, script-or-proxy)
        music      → flat RECIPE_MUSIC_CREDITS (v1; or 0 if off)
        recipe fee → RECIPE_FEE
    `catalog` is accepted for signature parity with the frontend FormatDef.price(catalog, input); the
    backend reads the live registries directly so badge == charge without a passed catalog."""
    import video_providers as _vp
    import image_providers as _ip
    import credit_catalog as _cat

    style = (input.get("style") or "showcase").lower()
    seconds = _norm_seconds(input.get("seconds"))
    n_variants = _variants(input)
    aspects = _aspects(input)
    n_hero, n_broll = _shot_count_for(seconds, style)
    per_hero = _per_clip_seconds(seconds, max(1, n_hero + n_broll))
    per_broll = per_hero
    want_vo = bool((input.get("voiceover") or {}).get("on"))
    want_music = bool((input.get("music") or {}).get("on"))

    hero_model, hero_feat = _hero_resolved(style)
    broll_model, broll_feat = _broll_resolved()

    # ── per-keyframe composite cost: scene_plate (create_raster) + harmonize (edit), per hero beat.
    # SAME number the run commits (via _kf_each_credits) → keyframe line is badge == charge.
    kf_each = _kf_each_credits()

    # ── per-clip render cost (badge == the metered video debit).
    hero_audio = _audio_on_for(hero_model) if hero_model else False
    broll_audio = _audio_on_for(broll_model) if broll_model else False
    hero_clip = (_vp.credits_for(hero_feat, hero_model, seconds=per_hero,
                                 resolution=RECIPE_CLIP_RES, audio_on=hero_audio) or 0) if hero_model else 0
    broll_clip = (_vp.credits_for(broll_feat, broll_model, seconds=per_broll,
                                  resolution=RECIPE_CLIP_RES, audio_on=broll_audio) or 0) if broll_model else 0

    # ── ugc avatars are AUDIO-DRIVEN: the avatar render itself lip-syncs the portrait to the voiceover,
    # so there is NO separate lip_sync op to price — the hero (avatar) clip above already carries it.

    # ── VO cost: price the script if present, else a length proxy (~16 chars/sec of speech).
    vo_credits = 0
    if want_vo:
        script = ((input.get("voiceover") or {}).get("script") or "").strip()
        proxy = script or ("x" * (seconds * 16))
        vo_credits = int(_cat.estimate_tts_credits(RECIPE_TTS_MODEL, proxy) or 0)

    # ── assemble per-variant per-aspect totals.
    line_items: list = []
    total = 0

    # keyframes: n_hero per variant (each = scene + harmonize image ops).
    kf_total = kf_each * n_hero * n_variants
    if kf_total:
        line_items.append({"label": f"Keyframes ({n_hero}×{n_variants}, locked product)", "credits": kf_total})
        total += kf_total

    # hero clips: n_hero per variant, rendered ONCE per variant (the same clip set is re-cropped to each
    # aspect in ffmpeg — assembly re-encodes but does NOT re-render the model, so clips are NOT ×aspects).
    hero_total = hero_clip * n_hero * n_variants
    if hero_total:
        line_items.append({
            "label": f"Hero clips · {hero_model} ({n_hero}×{n_variants}, {per_hero}s)",
            "credits": hero_total})
        total += hero_total

    broll_total = broll_clip * n_broll * n_variants
    if broll_total:
        line_items.append({
            "label": f"B-roll clips · {broll_model} ({n_broll}×{n_variants}, {per_broll}s)",
            "credits": broll_total})
        total += broll_total

    if vo_credits:
        # VO is synthesized ONCE and reused across variants (one script, one voice) → committed once,
        # so the estimate prices it once too (was ×n_variants → over-held → refunded as slack).
        line_items.append({"label": "Voiceover", "credits": vo_credits})
        total += vo_credits

    if want_music:
        music_total = RECIPE_MUSIC_CREDITS * n_variants
        line_items.append({"label": f"Music bed ({n_variants})", "credits": music_total})
        total += music_total

    # assembly (ffmpeg) is in-process + free (no upstream COGS) but we DO render once per aspect per
    # variant — that's CPU only, not metered. No line item.

    fee = _recipe_fee()
    line_items.append({"label": "Recipe fee", "credits": fee})
    total += fee

    return {"line_items": line_items, "total": int(total)}


# ══════════════════════════════ DAG sequencer ══════════════════════════════════
async def _host_bytes(data: bytes, mime: str, key_base: str) -> Optional[str]:
    """Upload bytes to object storage → SIGNED public URL. The atlascloud avatar models fetch their
    image/audio inputs server-side (their pricing probe rejects data: URIs), so the ugc path must host
    the portrait + voice and pass URLs. Returns None if storage is unavailable (R2 off) — the caller then
    drops the avatar clip rather than sending an unfetchable URI."""
    try:
        import storage
        if storage is None or not storage.is_configured():
            return None
        ext = {"audio/wav": "wav", "audio/mpeg": "mp3", "image/png": "png",
               "image/jpeg": "jpg", "video/mp4": "mp4"}.get(mime, "bin")
        key = await storage.aupload_bytes(f"recipe/{key_base}.{ext}", data, mime)
        return await storage.asigned_url(key)
    except Exception as e:
        log.info("_host_bytes(%s) failed: %s", key_base, e)
        return None


async def _animate_beat(beat: dict, style: str, cutouts: list, keyframe_bytes: Optional[bytes],
                        aspect: str, op_id: str, idx: int, audio_url: Optional[str] = None) -> Optional[dict]:
    """Animate ONE beat → a dispatch result dict (carries .data mp4 bytes). HERO beats animate the LOCKED
    keyframe (showcase=i2v on the keyframe; in_scene=reference_to_video with the cutouts as refs +
    keyframe seed; ugc=AUDIO-DRIVEN avatar — the portrait keyframe is lip-synced to `audio_url`). BROLL
    beats = text_to_video (loose, product absent). Returns None on a fully-failed dispatch (the caller
    decides whether that's fatal)."""
    import video_providers as _vp
    role = beat.get("role", "hero")
    seconds = int(beat.get("seconds") or RECIPE_HERO_MAX_SECONDS)
    motion = beat.get("motion") or ""
    scene_prompt = beat.get("scene_prompt") or ""
    prompt = f"{scene_prompt}. Camera: {motion}." if motion else scene_prompt

    if role == "broll":
        model, feat = _broll_resolved()
        params = {"prompt": prompt, "aspect": aspect, "seconds": seconds, "resolution": RECIPE_CLIP_RES}
        if model and _audio_on_for(model):
            params["audio"] = True
        return await _vp.dispatch(feat, model, params, f"{op_id}-broll{idx}")

    # HERO
    model, feat = _hero_resolved(style)
    if not model:
        return None

    # ── ugc: the hero is an AUDIO-DRIVEN avatar. It lip-syncs a hosted portrait to the hosted voiceover —
    # the avatar render IS the lip-sync (no separate lip_sync pass). atlascloud rejects data: URIs for the
    # avatar inputs, so host the portrait + use the pre-hosted `audio_url`.
    if style == "ugc":
        if keyframe_bytes is None or not audio_url:
            return None   # no portrait or no voice → can't drive the avatar; caller drops the clip
        img_url = await _host_bytes(keyframe_bytes, "image/png", f"{op_id}-ugcimg{idx}")
        if not img_url:
            return None   # hosting unavailable (R2 off) → avatar can't fetch a data: URI
        params = {"prompt": prompt, "resolution": RECIPE_CLIP_RES,
                  "ref_images": [{"url": img_url}], "audio_ref": {"url": audio_url}}
        return await _vp.dispatch(feat, model, params, f"{op_id}-hero{idx}")

    # Lock the product against the i2v model duplicating/morphing it mid-take (camera-only motion).
    lock = (" Keep EXACTLY ONE product, fixed in place — do NOT duplicate, split, clone, add, remove, "
            "morph, rotate or distort the product or its logo; no extra products, hands or people "
            "appear. Animate camera and lighting only.")
    params = {"prompt": prompt + lock, "aspect": aspect, "seconds": seconds, "resolution": RECIPE_CLIP_RES,
              "negative_prompt": ("duplicate product, two products, extra can, extra bottle, cloned object, "
                                  "warped logo, deformed product, extra hands, extra people, floating object, text artifacts")}
    if _audio_on_for(model):
        params["audio"] = True
    # seed the locked keyframe as the i2v first frame (showcase + ugc), and ALSO as a reference for
    # in_scene reference_to_video (cutouts join as additional refs so the product identity is enforced).
    refs = []
    if keyframe_bytes is not None:
        refs.append({"b64": base64.b64encode(keyframe_bytes).decode(), "mime": "image/png"})
    if feat == "reference_to_video":
        for c in cutouts[:2]:
            refs.append({"b64": base64.b64encode(c).decode(), "mime": "image/png"})
    if refs:
        params["ref_images"] = refs
    return await _vp.dispatch(feat, model, params, f"{op_id}-hero{idx}")


async def _lip_sync_clip(clip_ref: dict, vo_audio_bytes: Optional[bytes], op_id: str, idx: int) -> Optional[dict]:
    """UGC: lip-sync a hero avatar clip to the voiceover via the lip_sync op-chain (fal sync-lipsync).
    Best-effort — on any failure returns None and the caller keeps the un-synced clip (the avatar still
    moves; we just don't pay/charge for a failed sync). lip_sync needs a source VIDEO + an AUDIO track."""
    import video_providers as _vp
    if vo_audio_bytes is None:
        return None
    src = clip_ref.get("data")
    if not src:
        return None
    params = {
        "ref_video": {"b64": base64.b64encode(src).decode(), "mime": "video/mp4"},
        "audio": {"b64": base64.b64encode(vo_audio_bytes).decode(), "mime": "audio/wav"},
        "aspect": "9:16", "seconds": 5,
    }
    try:
        return await _vp.dispatch("lip_sync", "", params, f"{op_id}-lip{idx}")
    except Exception as e:
        log.info("lip_sync beat %d failed (non-fatal, keep raw avatar clip): %s", idx, e)
        return None


async def _synth_vo(script: str, voice: str, language: str) -> Optional[bytes]:
    """Synthesize the voiceover → WAV bytes, reusing the proven TTS paths (Gemini Vertex OAuth or the
    OpenAI-compatible /v1/audio/speech route) from laozhang_api. Best-effort: returns None on failure so
    the ad still assembles (silent / music-only). The CHARGE for TTS is reconciled by the umbrella
    hold/commit at the caller — this only produces the bytes."""
    script = (script or "").strip()
    if not script:
        return None
    synth = script[:4000]
    try:
        import laozhang_api as _api
        model = RECIPE_TTS_MODEL
        if _api._is_gemini_tts(model):
            chain = [model] + [m for m in _api.GEMINI_TTS_CHAIN if m != model]
            for _m in chain:
                try:
                    content = await asyncio.to_thread(_api._gemini_tts_oauth, _m, voice or "Zephyr", synth)
                    if content:
                        return content
                except Exception:
                    continue
            return None
        # OpenAI-compatible speech route (mirror _speak in /video/tts/scene).
        def _speak(m):
            import requests as _rq
            r = _rq.post("https://api.laozhang.ai/v1/audio/speech",
                         headers={"Authorization": f"Bearer {_api.API_KEY}", "Content-Type": "application/json"},
                         json={"model": m, "voice": voice or "alloy", "input": synth,
                               "speed": 1.0, "response_format": "wav"}, timeout=120)
            r.raise_for_status()
            return r.content
        try:
            return await asyncio.to_thread(lambda: _speak(model))
        except Exception:
            return await asyncio.to_thread(lambda: _speak("tts-1"))
    except Exception as e:
        log.info("VO synth failed (non-fatal): %s", e)
        return None


def _captions_from_script(script: str, total_seconds: int, chunk: int = 8) -> list:
    """Deterministic, evenly-timed caption windows from the VO script (NEVER generative — burned by
    ffmpeg). Splits the script into ~`chunk`-word groups spread across the ad length. `chunk` defaults
    to 8 (Product Ad block captions); narrow/social formats pass a smaller value (e.g. 5) so each line
    fits a vertical frame without overflow."""
    script = (script or "").strip()
    if not script:
        return []
    words = script.split()
    if not words:
        return []
    chunk = max(1, int(chunk))
    groups = [" ".join(words[i:i + chunk]) for i in range(0, len(words), chunk)]
    n = len(groups)
    if n == 0:
        return []
    span = max(0.5, total_seconds / n)
    return [{"t": round(i * span, 2), "text": g} for i, g in enumerate(groups)]


async def _auto_caption(input: dict, cutout_or_ref) -> str:
    """Vision auto-caption of the product when no product_desc is given. Best-effort, short. Uses the
    chat client with an image input (LaoZhang OpenAI-compatible vision). Returns '' on any failure (the
    DAG falls back to a generic description)."""
    try:
        import laozhang_api as _api
        model = os.getenv("RECIPE_CAPTION_MODEL", "gemini-2.5-flash")
        cl = _api.make_client(model)
        # cutout_or_ref is RGBA PNG bytes (the bg-removed product) → data URI for the vision input.
        if isinstance(cutout_or_ref, (bytes, bytearray)):
            data_uri = "data:image/png;base64," + base64.b64encode(bytes(cutout_or_ref)).decode()
        else:
            return ""
        msg = [{"role": "user", "content": [
            {"type": "text", "text": "In ONE short phrase (≤12 words), name and describe this product "
                                     "for an ad (type, color, brand if visible). No preamble."},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]}]
        r = await asyncio.to_thread(lambda: cl.chat.completions.create(
            model=model, messages=msg, temperature=0.3, max_tokens=60))
        cap = (r.choices[0].message.content or "").strip().strip('"')
        # the caption vision call's small chat COGS is absorbed by the flat recipe fee (no separate
        # ledger debit) — single charge mechanism, exact badge == charge. (Was debited on top before.)
        return " ".join(cap.split()[:14])
    except Exception as e:
        log.info("auto-caption failed (non-fatal): %s", e)
        return ""


async def run_product_ad_job(input: dict, op_id: str, set_progress) -> dict:
    """The full Product-Ad DAG sequencer for ALL THREE styles. Returns
        {"variants": [{"aspect", "key", "seconds", "credits"}], "credits": <committed_total>}

    DAG (contract §2):
      1. auto-caption the product (vision) if no product_desc.
      2. bg_remove each product image → RGBA cutouts (recipe_fidelity).
      3. per VARIANT: plan() → HERO beats: make_keyframes (locked, pixel-fidelity) → animate via
         video_providers.dispatch (showcase=i2v/FLF2V camera-static; in_scene=reference_to_video w/
         cutouts as refs; ugc=avatar i2v + lip_sync) ; BROLL beats: text_to_video (loose).
      4. VO: TTS(script) if voiceover.on.
      5. music: flat bed if music.on (v1) — gated cleanly when no library.
      6. assemble() per aspect → mp4 → _persist_asset.
      7. set_progress through phases for the poll UI.

    BILLING: ONE umbrella hold_credits(estimate.total, op_id); steps run via the dispatchers in-process;
    a final commit_credits(committed_total, op_id) settles the actual (commit < hold refunds the slack);
    every ledger line carries video_job=<job_id> → ONE history row; ANY failure refunds the whole hold.

    `op_id` is the server-minted umbrella id (recipe-<uuid>) — it is ALSO the video_job rollup key. The
    billing context (tenant/user/byok) is injected into `input` by the caller under _tenant_id /
    _user_id / _byok; _op_id is set here so plan()/auto-caption tag their metered chat with this same id.
    `set_progress` is an async callable set_progress(phase:str, pct:int, label:str) (the poll UI feed).
    """
    import video_providers as _vp
    import recipe_fidelity as _fid
    import recipe_assembly as _asm
    import laozhang_api as _api

    tenant_id = input.get("_tenant_id")
    user_id = input.get("_user_id")
    byok = bool(input.get("_byok"))
    input["_op_id"] = op_id   # so plan()/auto-caption tag their metered chat with the umbrella id

    async def _progress(phase: str, pct: int, label: str):
        try:
            if set_progress is not None:
                res = set_progress(phase, pct, label)
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            pass

    style = (input.get("style") or "showcase").lower()
    if style not in ("showcase", "in_scene", "ugc"):
        style = "showcase"
    vibe = (input.get("vibe") or "minimal").lower()
    aspects = _aspects(input)
    n_variants = _variants(input)
    seconds = _norm_seconds(input.get("seconds"))
    want_vo = bool((input.get("voiceover") or {}).get("on"))
    voice = (input.get("voiceover") or {}).get("voice") or ""
    language = (input.get("language") or "English").strip()
    want_music = bool((input.get("music") or {}).get("on"))
    music_bed = (input.get("music") or {}).get("bed")
    want_captions = bool(input.get("captions"))
    cta = (input.get("cta") or "").strip() or None
    brand = input.get("brand") or None
    scene_prompt = (input.get("scene_prompt") or "").strip() or None
    product_images = input.get("product_images") or []
    if not product_images:
        raise ValueError("run_product_ad_job: at least one product image is required")

    # ── umbrella hold (the single source = estimate.total) ─────────────────────
    est = estimate(input)
    held = est["total"]
    await _api.metering.hold_credits(tenant_id, held, op_id, byok=byok)

    committed_total = 0
    variants_out: list = []
    try:
        # 1. auto-caption (vision) if no product_desc.
        await _progress("prep", 3, "Reading your product…")
        if not (input.get("product_desc") or "").strip():
            # bg_remove the FIRST image early so the caption sees a clean cutout.
            try:
                first_cut = await _fid.bg_remove(product_images[0], f"{op_id}-bg0")
                cap = await _auto_caption(input, first_cut)
                if cap:
                    input["product_desc"] = cap
                _pre_cut0 = first_cut
            except Exception as e:
                log.info("early bg/caption failed (non-fatal): %s", e)
                _pre_cut0 = None
        else:
            _pre_cut0 = None

        # 2. bg_remove each product image → cutouts (reuse the early one for image 0).
        await _progress("cutout", 10, "Isolating the product…")
        cutouts: list = []
        for i, img in enumerate(product_images[:4]):
            if i == 0 and _pre_cut0 is not None:
                cutouts.append(_pre_cut0)
                continue
            try:
                cutouts.append(await _fid.bg_remove(img, f"{op_id}-bg{i}"))
            except Exception as e:
                log.info("bg_remove image %d failed (non-fatal): %s", i, e)
        if not cutouts:
            raise RuntimeError("background removal failed for all product images")

        # one shared VO for the ad (re-used across variants/aspects — same script, same voice).
        # vo_committed guards a SINGLE TTS commit no matter which path synthesizes it first (the ugc
        # hero loop synthesizes early; non-ugc synthesizes in the dedicated VO step) — previously ugc
        # synthesized in the hero loop and the VO-step commit was then skipped, so ugc VO went unbilled.
        vo_audio_bytes: Optional[bytes] = None
        vo_committed = False

        # 3–6. per VARIANT.
        for v in range(n_variants):
            base_pct = 15 + int(60 * v / max(1, n_variants))
            await _progress("plan", base_pct, f"Planning variant {v + 1}/{n_variants}…")
            v_plan = await plan(input)
            beats = v_plan["beats"]
            script = v_plan.get("script") or ""

            # 3a. HERO keyframes (locked product, deterministic paste-back) per hero beat.
            hero_beats = [b for b in beats if b["role"] == "hero"]
            broll_beats = [b for b in beats if b["role"] == "broll"]
            await _progress("keyframes", base_pct + 4, f"Building hero frames (variant {v + 1})…")
            # make_keyframes orchestrates scene_plate→place→harmonize→paste_back→gate; one frame/hero
            # beat. Use the first aspect for the keyframe plate (clips re-crop per aspect in assembly).
            kf_aspect = aspects[0]
            keyframes = await _fid.make_keyframes(
                style, cutouts, scene_prompt, vibe, kf_aspect, max(1, len(hero_beats)),
                f"{op_id}-v{v}")
            kf_bytes = [k["bytes"] for k in keyframes]
            # commit the keyframe image COGS (scene_plate + harmonize, per frame ACTUALLY built) — the
            # same basis estimate held. Charging for frames built (not requested) keeps commit ≤ hold
            # if a keyframe was dropped by the fault-tolerant gather.
            committed_total += _kf_each_credits() * len(keyframes)

            # 3b. animate beats CONCURRENTLY. The clips are independent renders; doing them one at a
            # time was the dominant wall-clock cost (N clips × ~2 min each, cores idle). Fan the model
            # calls out under a small semaphore (so we don't blow one provider's concurrency limit),
            # then commit / lip-sync in deterministic shooting order so billing + ordering are byte-for-
            # byte identical to the old sequential path.
            await _progress("render", base_pct + 10, f"Rendering clips (variant {v + 1})…")
            _clip_sem = asyncio.Semaphore(max(1, _CLIP_CONCURRENCY))

            # ugc heroes are AUDIO-DRIVEN avatars: the voiceover must be synthesized AND hosted BEFORE the
            # avatar render (the avatar gen IS the lip-sync — there is no separate lip_sync pass). Synth +
            # host the shared VO once here, ahead of the concurrent render; the hosted URL drives each beat.
            ugc_audio_url = None
            if style == "ugc" and want_vo and hero_beats:
                if vo_audio_bytes is None:
                    vo_audio_bytes = await _synth_vo(script, voice, language)
                    if vo_audio_bytes is not None and not vo_committed:
                        committed_total += _commit_tts(script, tenant_id, user_id, op_id, byok, _api)
                        vo_committed = True
                if vo_audio_bytes is not None:
                    ugc_audio_url = await _host_bytes(vo_audio_bytes, "audio/wav", f"{op_id}-v{v}-vo")

            async def _animate_guarded(beat, kf, idx):
                async with _clip_sem:
                    return await _animate_beat(beat, style, cutouts, kf, kf_aspect, f"{op_id}-v{v}", idx,
                                               audio_url=(ugc_audio_url if style == "ugc" else None))

            hero_tasks = [
                _animate_guarded(beat, (kf_bytes[hi % len(kf_bytes)] if kf_bytes else None), hi)
                for hi, beat in enumerate(hero_beats)
            ]
            broll_tasks = [_animate_guarded(beat, None, bi) for bi, beat in enumerate(broll_beats)]
            hero_res, broll_res = await asyncio.gather(
                asyncio.gather(*hero_tasks, return_exceptions=True),
                asyncio.gather(*broll_tasks, return_exceptions=True),
            )

            clip_results: list = []   # ordered: heroes then brolls (shooting order)
            for hi, (beat, res) in enumerate(zip(hero_beats, hero_res)):
                if isinstance(res, BaseException) or res is None or res.get("data") is None:
                    continue
                # ugc avatar clips already carry the synced voiceover (the avatar IS the lip-sync), so there
                # is no separate lip_sync commit — the clip is final.
                committed_total += _commit_clip(res, beat, tenant_id, user_id, op_id, byok, _api,
                                                 _hero_resolved(style))
                clip_results.append(res)
            for bi, (beat, res) in enumerate(zip(broll_beats, broll_res)):
                if isinstance(res, BaseException) or res is None or res.get("data") is None:
                    continue
                committed_total += _commit_clip(res, beat, tenant_id, user_id, op_id, byok, _api,
                                                _broll_resolved())
                clip_results.append(res)

            if not clip_results:
                raise RuntimeError(f"variant {v + 1}: all clips failed to render")

            # 4. VO (shared; synth once across variants if not already done).
            if want_vo and vo_audio_bytes is None:
                await _progress("voiceover", base_pct + 14, "Recording voiceover…")
                vo_audio_bytes = await _synth_vo(script, voice, language)
                if vo_audio_bytes is not None and not vo_committed:
                    committed_total += _commit_tts(script, tenant_id, user_id, op_id, byok, _api)
                    vo_committed = True

            # 5. music: caller-supplied bed, else generate an instrumental bed via fal (best-effort).
            music_input = None
            if want_music:
                if music_bed:
                    music_input = {"url": music_bed} if str(music_bed).startswith("http") else music_bed
                else:
                    await _progress("music", base_pct + 16, "Scoring the music…")
                    _pdesc = (input.get("product_desc") or "a premium product").strip()
                    _mprompt = (f"{vibe} modern commercial advertising background music for {_pdesc}, "
                                f"instrumental, no vocals, clean, upbeat, broadcast-ready")
                    music_input = await _gen_music(_mprompt, seconds)
                if music_input is not None:
                    committed_total += _commit_music(tenant_id, user_id, op_id, byok, _api)
                else:
                    log.info("music requested but generation unavailable → silent")

            # 6. assemble per aspect → mp4 → persist.
            clip_bytes = [{"bytes": r["data"]} for r in clip_results]
            captions = _captions_from_script(script, seconds) if (want_captions and script) else None
            for aspect in aspects:
                await _progress("assemble", base_pct + 18,
                                f"Editing {aspect} (variant {v + 1})…")
                try:
                    mp4 = await _asm.assemble(
                        clip_bytes, aspect,
                        vo_audio=vo_audio_bytes, music_bed=music_input,
                        captions=captions, cta=cta, brand=brand)
                except Exception as e:
                    log.warning("assemble %s variant %d failed: %s", aspect, v, e)
                    continue
                key = await _persist_variant(tenant_id, user_id, op_id, v, aspect, mp4, input, _api)
                variants_out.append({
                    "aspect": aspect, "key": key, "seconds": seconds,
                    "credits": 0,   # per-clip credits already committed at the umbrella level
                })

        if not variants_out:
            raise RuntimeError("no variants produced")

        # ── final reconcile: commit the actual (commit < hold auto-refunds the slack). One row.
        # Add the flat recipe fee (the recipe's value-add; it also absorbs the planner/caption chat COGS
        # that is no longer separately metered) so the umbrella commit matches the displayed estimate.
        await _progress("finalize", 96, "Finalizing…")
        committed_total += _recipe_fee()
        committed_total = min(int(committed_total), int(held))
        await _api.metering.commit_credits(
            tenant_id, user_id, "video", "wimba-product-ad", committed_total, op_id,
            byok=byok, video_job=op_id, write_log=True)
        await _progress("done", 100, "Done")
        return {"variants": variants_out, "credits": committed_total}
    except BaseException as e:
        # ANY failure → release the WHOLE umbrella hold (nothing committed against op_id yet — commit is
        # the single terminal step, so refund_credits cleanly releases the reservation).
        await _api.metering.refund_credits(tenant_id, op_id)
        log.info("run_product_ad_job %s failed → refunded hold: %s", op_id, e)
        raise


# ── per-step commit accounting helpers (sum into committed_total; the umbrella commit posts ONCE) ──
# These do NOT charge the ledger individually — the umbrella commit_credits(committed_total) is the
# single ledger post. They only ACCUMULATE the real per-step credit the badge promised, so commit ==
# what was actually rendered (and commit < hold refunds anything that failed to render).
def _commit_clip(res: dict, beat: dict, tenant_id, user_id, op_id, byok, _api, resolved: tuple) -> int:
    import video_providers as _vp
    model, feat = resolved
    secs = int(beat.get("seconds") or RECIPE_HERO_MAX_SECONDS)
    audio_on = _audio_on_for(model) if model else False
    return int(_vp.credits_for(feat, model, seconds=secs, resolution=RECIPE_CLIP_RES,
                               audio_on=audio_on) or 0)


def _commit_liptool(res: dict, beat: dict, tenant_id, user_id, op_id, byok, _api) -> int:
    import video_providers as _vp
    secs = int(beat.get("seconds") or RECIPE_HERO_MAX_SECONDS)
    return int(_vp.credits_for("lip_sync", "", seconds=secs) or 0)


def _commit_tts(script: str, tenant_id, user_id, op_id, byok, _api) -> int:
    import credit_catalog as _cat
    return int(_cat.estimate_tts_credits(RECIPE_TTS_MODEL, (script or "")[:4000]) or 0)


def _commit_music(tenant_id, user_id, op_id, byok, _api) -> int:
    return RECIPE_MUSIC_CREDITS


async def _gen_music(prompt: str, seconds: int) -> Optional[bytes]:
    """Generate an instrumental commercial background bed via fal (RECIPE_MUSIC_MODEL, default
    cassetteai/music-generator — instrumental, no lyrics). Best-effort: returns audio bytes or None
    (the ad stays silent / VO-only). fal queue: submit → poll status_url → fetch response_url → download.
    Charged via _commit_music only when a bed actually comes back."""
    import httpx as _httpx, asyncio as _asyncio
    key = os.environ.get("FAL_API_KEY")
    if not key:
        return None
    h = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    body = {"prompt": prompt, "duration": max(8, int(seconds or 15))}
    base = f"https://queue.fal.run/{RECIPE_MUSIC_MODEL}"
    try:
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
                                      follow_redirects=True) as c:
            r = await c.post(base, headers=h, json=body); r.raise_for_status()
            j = r.json() or {}
            status_url, response_url = j.get("status_url"), j.get("response_url")
            if not (status_url and response_url):
                return None
            loop = _asyncio.get_event_loop(); deadline = loop.time() + RECIPE_MUSIC_POLL_MAX
            while loop.time() < deadline:
                s = await c.get(status_url, headers=h, timeout=15.0); s.raise_for_status()
                st = (s.json() or {}).get("status")
                if st == "COMPLETED":
                    break
                if st in ("FAILED", "ERROR"):
                    return None
                await _asyncio.sleep(2.0)
            else:
                return None
            out = await c.get(response_url, headers=h); out.raise_for_status()
            od = out.json() or {}
            url = ((od.get("audio") or {}).get("url") or od.get("audio_url")
                   or (od.get("audio_file") or {}).get("url"))
            if not url:
                return None
            a = await c.get(url, timeout=60.0); a.raise_for_status()
            return a.content
    except Exception as e:
        log.warning("music-gen failed (non-fatal): %s", e)
        return None


async def _persist_variant(tenant_id, user_id, op_id, v: int, aspect: str, mp4: bytes,
                           input: dict, _api) -> Optional[str]:
    """Persist ONE finished variant mp4 to R2 + assets (Media Vault moat). source_job_type MUST be a
    valid job_type_enum value (oneshot_fix|batch_image|tts|imagen|veo|sora|narasi|generate_image|whisk|
    flow_storyboard|flow_image|script_tts) or the INSERT raises 'invalid input value for enum' → the row
    is silently dropped (the documented Vault-persistence bug). 'recipe_product_ad' is NOT in the enum,
    so we persist under the valid video label 'veo' and carry the recipe identity in metadata. Returns
    the R2 KEY (signed on read) or None (best-effort — never fails the render)."""
    aspect_tag = aspect.replace(":", "x")
    fname = f"{op_id}-v{v}-{aspect_tag}.mp4"
    try:
        await _api._persist_asset(
            tenant_id, asset_type="video", filename=fname, data=mp4,
            content_type="video/mp4", source_job_type="veo",   # valid enum; recipe id lives in metadata
            user_id=user_id,
            source_prompt=(input.get("product_desc") or None),
            metadata={"recipe": "product_ad", "style": (input.get("style") or "showcase"),
                      "vibe": input.get("vibe"), "aspect": aspect, "variant": v,
                      "seconds": _norm_seconds(input.get("seconds")), "job_id": op_id})
    except Exception as e:
        log.warning("persist variant v%d %s failed (non-fatal): %s", v, aspect, e)
    # The R2 KEY for the signed-on-read URL: build the same key _persist_asset used.
    try:
        import storage
        if storage.is_configured():
            return storage.build_key(tenant_id, None, "video", fname)
    except Exception:
        pass
    return None

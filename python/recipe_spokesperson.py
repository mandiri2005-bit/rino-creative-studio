"""Wimba SPOKESPERSON recipe — Phase 1 MVP.

A script (or a topic) → a finished talking-head AI-presenter video: an audio-driven avatar (OmniHuman)
lip-syncs the voiceover, with burned captions + brand + optional music. Phase 1 = a SINGLE presenter clip
(no b-roll yet) — the avatar IS the lip-sync (no separate lip_sync pass). The voiceover is the spine:
we synth it once, the avatar is driven by it, and assembly re-muxes the SAME VO so the lip-sync aligns.

Reuses the Product Ad machinery (helpers imported from recipe_product_ad): umbrella hold/commit billing,
TTS (_synth_vo), R2 hosting (_host_bytes), the avatar dispatch + failover chain, recipe_assembly.assemble,
and the per-variant persist. New here: presenter resolution (generate | upload | preset) + a tiny script
planner. Contract identical to Product Ad: ONE hold(estimate) → per-step commit → commit<hold refunds;
video_job=op_id → one history row; any failure refunds the whole hold. Badge == charge.

Phase 1 keeps the whole script within ONE avatar render, so we clamp length to the provider duration cap
(OmniHuman 720p ≤ ~60s). Longer scripts + b-roll interleave land in Phase 2 (per the spec)."""
from __future__ import annotations
import os
import base64
import logging
import asyncio
from typing import Optional, Any

from recipe_product_ad import (
    _recipe_fee, _norm_seconds, _aspects, _variants, _audio_on_for,
    _host_bytes, _synth_vo, _commit_tts, _commit_music, _gen_music, _captions_from_script,
)

log = logging.getLogger("recipe_spokesperson")

# ── tunables ────────────────────────────────────────────────────────────────────
# Phase 1 renders ONE avatar clip for the whole script → clamp to the avatar provider's audio cap.
SPK_MAX_SECONDS = int(os.getenv("SPK_MAX_SECONDS", "45"))
SPK_AVATAR_MODEL = os.getenv("SPK_AVATAR_MODEL", "omnihuman-avatar")   # chain[0]=atlascloud (cheapest)
SPK_AVATAR_FEATURE = "image_to_video"
SPK_PORTRAIT_MODEL = os.getenv("SPK_PORTRAIT_MODEL", "nano-banana")    # presenter portrait gen
SPK_CLIP_RES = os.getenv("SPK_CLIP_RES", "720p")
RECIPE_PLAN_MODEL = os.getenv("RECIPE_PLAN_MODEL", "claude-sonnet-4-6")
SPK_MAX_IMG_BYTES = int(os.getenv("SPK_MAX_IMG_BYTES", str(15 * 1024 * 1024)))

# Preset presenter gallery (Phase 1: fixed looks → generated with a stable prompt, no stored assets yet).
_PRESETS = {
    "host_warm_f":      "a friendly professional woman in her late 20s, warm genuine smile",
    "host_confident_m": "a confident professional man in his 30s, approachable",
    "creator_casual_f": "a casual young female content creator, relatable and upbeat",
    "creator_casual_m": "a casual young male content creator, relatable and upbeat",
    "narrator_neutral": "a calm neutral presenter, clean professional look",
}
_PORTRAIT_SUFFIX = ("head-and-shoulders portrait, facing the camera straight on, soft studio lighting, "
                    "plain neutral background, photorealistic, sharp focus, no text, no logo")


def _spk_seconds(input: dict) -> int:
    """Requested length, clamped to the single-render avatar cap (Phase 1)."""
    return min(_norm_seconds(input.get("seconds")), SPK_MAX_SECONDS)


# ── presenter resolution (generate | upload | preset) → portrait BYTES ─────────────
async def _resolve_presenter(input: dict, op_id: str) -> Optional[bytes]:
    """Return the presenter PORTRAIT as PNG/JPEG bytes. source:
      generate → image pipeline from gender/age/vibe ; upload → user image (data-uri | http url) ;
      preset   → a fixed gallery look (generated with a stable prompt). Best-effort: None on failure
      (caller then fails the job + refunds — the presenter is the core of this format)."""
    import image_providers as _ip
    p = input.get("presenter") or {}
    source = (p.get("source") or "generate").lower()

    if source == "upload":
        img = p.get("image") or ""
        # decode a data-uri or fetch an http(s) url → portrait bytes (size-capped).
        try:
            if isinstance(img, str) and (img.startswith("http://") or img.startswith("https://")):
                import httpx
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await c.get(img); r.raise_for_status()
                    return r.content[:SPK_MAX_IMG_BYTES] if len(r.content) else None
            b64 = img.split(",")[-1] if isinstance(img, str) else ""
            return base64.b64decode(b64) if b64 else None
        except Exception as e:
            log.info("presenter upload decode failed: %s", e)
            return None

    # generate / preset → build a portrait prompt then run the image pipeline.
    if source == "preset":
        base_look = _PRESETS.get(p.get("preset_id") or "", _PRESETS["narrator_neutral"])
    else:
        gender = (p.get("gender") or "person").strip()
        age = (p.get("age") or "young adult").strip()
        vibe = (p.get("vibe") or input.get("vibe") or "friendly").strip()
        base_look = f"a {vibe} {age} {gender} presenter"
    prompt = f"{base_look}, {_PORTRAIT_SUFFIX}"
    try:
        res = await _ip.dispatch("create_raster", SPK_PORTRAIT_MODEL,
                                 {"prompt": prompt, "aspect": "1:1", "n": 1}, f"{op_id}-presenter")
        return res.get("data")
    except Exception as e:
        log.info("presenter generation failed: %s", e)
        return None


def _presenter_is_generated(input: dict) -> bool:
    return (((input.get("presenter") or {}).get("source") or "generate").lower()) != "upload"


# ── pricing helpers (badge == charge: estimate + commit read the SAME meters) ───────
def _presenter_credits() -> int:
    import image_providers as _ip
    return int(_ip.credits_for("create_raster", SPK_PORTRAIT_MODEL) or 0)


def _avatar_credits(seconds: int) -> int:
    import video_providers as _vp
    return int(_vp.credits_for(SPK_AVATAR_FEATURE, SPK_AVATAR_MODEL, seconds=seconds,
                               resolution=SPK_CLIP_RES, audio_on=_audio_on_for(SPK_AVATAR_MODEL)) or 0)


def _vo_credits(script: str, seconds: int) -> int:
    import credit_catalog as _cat
    proxy = (script or "").strip() or ("x" * (seconds * 16))
    return int(_cat.estimate_tts_credits(SPK_TTS_MODEL, proxy[:4000]) or 0)


SPK_TTS_MODEL = os.getenv("SPK_TTS_MODEL", os.getenv("RECIPE_TTS_MODEL", "gemini-3.1-flash-tts-preview"))
SPK_MUSIC_CREDITS = int(os.getenv("SPK_MUSIC_CREDITS", os.getenv("RECIPE_MUSIC_CREDITS", "5")))


# ── script planner (Phase 1: produce the spoken script) ────────────────────────────
async def plan(input: dict) -> dict:
    """Return {"script": <spoken words>}. If the user pasted a script, use it; else write one timed to
    `seconds` from the topic (one capped LLM call). Best-effort: any failure → a minimal deterministic
    script so the render is never blocked."""
    seconds = _spk_seconds(input)
    vo = input.get("voiceover") or {}
    given = (vo.get("script") or "").strip()
    if given:
        return {"script": given[:4000]}

    topic = (input.get("topic") or input.get("product_desc") or "").strip()
    language = (input.get("language") or "English").strip()
    cta = (input.get("cta") or "").strip()
    vibe = (input.get("vibe") or "friendly").lower()
    if not topic:
        return {"script": ""}
    prompt = (
        "You are a short-form video scriptwriter. Write ONLY the spoken words (no scene directions, no "
        "labels, no quotes) for a single on-camera presenter, in " + language + ", " + vibe + " tone, "
        "timed to about " + str(seconds) + " seconds of speech (~" + str(int(seconds * 2.2)) + " words). "
        "Open with a strong hook in the first sentence" + (", end on this call-to-action: '" + cta + "'."
        if cta else ".") + " Topic: " + topic + ".\nOutput ONLY the script text.")
    try:
        import laozhang_api as _api
        cl = _api.make_client(RECIPE_PLAN_MODEL)
        mt = min(700, _api.MODEL_MAX_TOKENS.get(_api.MODELS.get(RECIPE_PLAN_MODEL, RECIPE_PLAN_MODEL),
                                                _api.DEFAULT_MAX_TOKENS))
        r = await asyncio.to_thread(lambda: cl.chat.completions.create(
            model=RECIPE_PLAN_MODEL, messages=[{"role": "user", "content": prompt}],
            temperature=0.7, max_tokens=mt))
        txt = (r.choices[0].message.content or "").strip().strip('"')
        return {"script": txt[:4000] if txt else (f"{topic}. {cta}".strip() if cta else topic)}
    except Exception as e:
        log.info("spokesperson plan LLM failed (%s) → minimal script", e)
        return {"script": (f"{topic}. {cta}".strip() if cta else topic)[:4000]}


# ── estimate (single source for the /estimate receipt AND the umbrella hold) ────────
def estimate(input: dict, catalog: Any = None) -> dict:
    seconds = _spk_seconds(input)
    n_variants = _variants(input)
    want_music = bool((input.get("music") or {}).get("on"))
    given_script = ((input.get("voiceover") or {}).get("script") or "").strip()

    line_items: list = []
    total = 0

    if _presenter_is_generated(input):
        # the portrait is generated ONCE and reused across variants → price it ×1 (committed once).
        pc = _presenter_credits()
        if pc:
            line_items.append({"label": "Presenter portrait", "credits": pc})
            total += pc

    av = _avatar_credits(seconds)
    if av:
        line_items.append({"label": f"Presenter · {SPK_AVATAR_MODEL} ({seconds}s × {n_variants})",
                           "credits": av * n_variants})
        total += av * n_variants

    voc = _vo_credits(given_script, seconds)
    if voc:
        # VO is per-variant here (each variant is its own full render); priced ×n_variants.
        line_items.append({"label": f"Voiceover ({n_variants})", "credits": voc * n_variants})
        total += voc * n_variants

    if want_music:
        line_items.append({"label": f"Music bed ({n_variants})", "credits": SPK_MUSIC_CREDITS * n_variants})
        total += SPK_MUSIC_CREDITS * n_variants

    fee = _recipe_fee()
    line_items.append({"label": "Recipe fee", "credits": fee})
    total += fee
    return {"line_items": line_items, "total": int(total)}


# ── the DAG sequencer ───────────────────────────────────────────────────────────
async def run_spokesperson_job(input: dict, op_id: str, set_progress) -> dict:
    """Spokesperson Phase 1: presenter portrait → VO → avatar render → assemble → persist.
    ONE umbrella hold(estimate) → per-step commit → commit<hold refunds; any failure refunds the hold."""
    import video_providers as _vp
    import recipe_assembly as _asm
    import laozhang_api as _api

    tenant_id = input.get("_tenant_id")
    user_id = input.get("_user_id")
    byok = bool(input.get("_byok"))
    input["_op_id"] = op_id

    async def _progress(phase: str, pct: int, label: str):
        try:
            if set_progress is not None:
                r = set_progress(phase, pct, label)
                if asyncio.iscoroutine(r):
                    await r
        except Exception:
            pass

    aspects = _aspects(input)
    n_variants = _variants(input)
    seconds = _spk_seconds(input)
    voice = (input.get("voiceover") or {}).get("voice") or ""
    language = (input.get("language") or "English").strip()
    want_music = bool((input.get("music") or {}).get("on"))
    music_bed = (input.get("music") or {}).get("bed")
    want_captions = bool(input.get("captions", True))
    cta = (input.get("cta") or "").strip() or None
    brand = input.get("brand") or None

    est = estimate(input)
    held = est["total"]
    await _api.metering.hold_credits(tenant_id, held, op_id, byok=byok)

    committed_total = 0
    variants_out: list = []
    try:
        # 1. presenter portrait → host (hosted URL required by the avatar models).
        await _progress("presenter", 8, "Casting your presenter…")
        portrait = await _resolve_presenter(input, op_id)
        if portrait is None:
            raise RuntimeError("could not resolve a presenter portrait")
        if _presenter_is_generated(input):
            committed_total += _presenter_credits()
        portrait_url = await _host_bytes(portrait, "image/png", f"{op_id}-presenter")
        if not portrait_url:
            raise RuntimeError("presenter hosting unavailable (R2 off)")

        for v in range(n_variants):
            base_pct = 18 + int(64 * v / max(1, n_variants))

            # 2. script + voiceover (the spine).
            await _progress("script", base_pct, f"Writing the script (variant {v + 1})…")
            script = (await plan(input)).get("script") or ""
            await _progress("voice", base_pct + 6, f"Recording the voiceover (variant {v + 1})…")
            vo_bytes = await _synth_vo(script, voice, language)
            if vo_bytes is None:
                raise RuntimeError("voiceover synthesis failed (a spokesperson needs a voice)")
            committed_total += _commit_tts(script, tenant_id, user_id, op_id, byok, _api)
            vo_url = await _host_bytes(vo_bytes, "audio/wav", f"{op_id}-v{v}-vo")
            if not vo_url:
                raise RuntimeError("voiceover hosting unavailable (R2 off)")

            # 3. avatar render — the talking-head presenter (audio-driven; the avatar IS the lip-sync).
            await _progress("render", base_pct + 14, f"Filming the presenter (variant {v + 1})…")
            talk_prompt = "a presenter speaking naturally to the camera, subtle head and hand motion"
            res = await _vp.dispatch(SPK_AVATAR_FEATURE, SPK_AVATAR_MODEL,
                                     {"prompt": talk_prompt, "resolution": SPK_CLIP_RES,
                                      "ref_images": [{"url": portrait_url}], "audio_ref": {"url": vo_url}},
                                     f"{op_id}-v{v}-presenter")
            if res is None or res.get("data") is None:
                raise RuntimeError(f"variant {v + 1}: presenter render failed")
            committed_total += _avatar_credits(seconds)
            clip = res["data"]

            # 4. music (optional) → ducked under the VO in assembly.
            music_input = None
            if want_music:
                if music_bed:
                    music_input = {"url": music_bed} if str(music_bed).startswith("http") else music_bed
                else:
                    await _progress("music", base_pct + 18, "Scoring the music…")
                    _mprompt = (f"{(input.get('vibe') or 'friendly')} modern background music for a "
                                f"talking-head video, instrumental, no vocals, clean, broadcast-ready")
                    music_input = await _gen_music(_mprompt, seconds)
                if music_input is not None:
                    committed_total += _commit_music(tenant_id, user_id, op_id, byok, _api)
                else:
                    log.info("music requested but unavailable → silent")

            # 5. assemble per aspect — the avatar clip lip-syncs the VO; re-mux the SAME VO + burn
            #    captions + brand (the avatar's baked audio is replaced by the identical master VO so
            #    timing is exact). Phase 1 = the single presenter clip fills the frame.
            captions = _captions_from_script(script, seconds) if (want_captions and script) else None
            for aspect in aspects:
                await _progress("assemble", base_pct + 22, f"Editing {aspect} (variant {v + 1})…")
                try:
                    mp4 = await _asm.assemble([{"bytes": clip}], aspect, vo_audio=vo_bytes,
                                              music_bed=music_input, captions=captions, cta=cta, brand=brand)
                except Exception as e:
                    log.warning("assemble %s v%d failed: %s", aspect, v, e)
                    continue
                key = await _persist_variant(tenant_id, user_id, op_id, v, aspect, mp4, input, _api)
                variants_out.append({"aspect": aspect, "key": key, "seconds": seconds, "credits": 0})

        if not variants_out:
            raise RuntimeError("no variants produced")

        await _progress("finalize", 96, "Finalizing…")
        committed_total += _recipe_fee()
        committed_total = min(int(committed_total), int(held))
        await _api.metering.commit_credits(
            tenant_id, user_id, "video", "wimba-spokesperson", committed_total, op_id,
            byok=byok, video_job=op_id, write_log=True)
        await _progress("done", 100, "Done")
        return {"variants": variants_out, "credits": committed_total}
    except BaseException as e:
        await _api.metering.refund_credits(tenant_id, op_id)
        log.info("run_spokesperson_job %s failed → refunded hold: %s", op_id, e)
        raise


async def _persist_variant(tenant_id, user_id, op_id, v: int, aspect: str, mp4: bytes,
                           input: dict, _api) -> Optional[str]:
    """Persist ONE finished mp4 to R2 + assets (Media Vault). source_job_type MUST be a valid
    job_type_enum value ('veo' is used; the recipe identity lives in metadata). Best-effort → key | None."""
    fname = f"{op_id}-v{v}-{aspect.replace(':', 'x')}.mp4"
    try:
        await _api._persist_asset(
            tenant_id, asset_type="video", filename=fname, data=mp4, content_type="video/mp4",
            source_job_type="veo", user_id=user_id,
            source_prompt=((input.get("topic") or input.get("product_desc")) or None),
            metadata={"recipe": "spokesperson", "vibe": input.get("vibe"), "aspect": aspect,
                      "variant": v, "seconds": _spk_seconds(input), "job_id": op_id})
    except Exception as e:
        log.info("spokesperson persist v%d %s failed (non-fatal): %s", v, aspect, e)
        return None
    return f"recipe/{fname}"

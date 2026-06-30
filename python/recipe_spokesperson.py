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


def _avatar_credits(seconds: int, cogs_override: float = None) -> int:
    import video_providers as _vp
    return int(_vp.credits_for(SPK_AVATAR_FEATURE, SPK_AVATAR_MODEL, seconds=seconds,
                               resolution=SPK_CLIP_RES, audio_on=_audio_on_for(SPK_AVATAR_MODEL),
                               cogs_override=cogs_override) or 0)


def _avatar_worst_cogs() -> Optional[float]:
    """Worst-case (max KNOWN) per-second COGS across the avatar model's provider chain — the HOLD basis
    (option A) so a failover to the priciest provider is always covered. None → catalog uncovered (the
    hold then uses the registry cheapest cogs, i.e. the legacy behaviour)."""
    try:
        import pricing as _pricing
        return _pricing.bounds_any(SPK_AVATAR_MODEL, SPK_AVATAR_FEATURE).get("max_cost")
    except Exception:
        return None


def _avatar_credits_hold(seconds: int) -> int:
    """HOLD = worst-case provider basis (option A): reserve enough that ANY failover stays profitable."""
    return _avatar_credits(seconds, cogs_override=_avatar_worst_cogs())


def _avatar_credits_actual(cost_per_sec, seconds: int) -> int:
    """COMMIT = the provider that ACTUALLY served (dispatch.cost_usd) → fair charge, refund the slack vs the
    worst-case hold. Falls back to the registry cheapest cogs if the actual cost is unknown."""
    try:
        c = float(cost_per_sec)
    except (TypeError, ValueError):
        c = None
    return _avatar_credits(seconds, cogs_override=(c if (c and c > 0) else None))


def _vo_credits(script: str, seconds: int) -> int:
    import credit_catalog as _cat
    proxy = (script or "").strip() or ("x" * (seconds * 16))
    return int(_cat.estimate_tts_credits(SPK_TTS_MODEL, proxy[:4000]) or 0)


SPK_TTS_MODEL = os.getenv("SPK_TTS_MODEL", os.getenv("RECIPE_TTS_MODEL", "gemini-3.1-flash-tts-preview"))
SPK_MUSIC_CREDITS = int(os.getenv("SPK_MUSIC_CREDITS", os.getenv("RECIPE_MUSIC_CREDITS", "5")))

# ── Phase 2 (b-roll interleave) — additive, gated by input.broll.on (default OFF → Phase 1 preserved) ──
SPK_BROLL_MODEL = os.getenv("SPK_BROLL_MODEL", "seedance-2-mini")        # cheap text_to_video cutaways
SPK_PRESENTER_RATIO = float(os.getenv("SPK_PRESENTER_RATIO", "0.6"))     # estimate split presenter/b-roll
SPK_MAX_SEGMENTS = int(os.getenv("SPK_MAX_SEGMENTS", "8"))
# Margin guard: cap total rendered seconds at requested × tolerance so a planner that over-writes the
# script can't drive actual COGS far past the held estimate (commit clamps to hold → WE'd eat the excess).
SPK_DURATION_TOLERANCE = float(os.getenv("SPK_DURATION_TOLERANCE", "1.5"))
# Caption words-per-line — short for the vertical social-native look (avoids the 9:16 overflow that a
# wider 8-word line hits at the karaoke font size). libass also wraps (WrapStyle) as a safety net.
SPK_CAPTION_WORDS = int(os.getenv("SPK_CAPTION_WORDS", "5"))


def _broll_on(input: dict) -> bool:
    return bool((input.get("broll") or {}).get("on"))


def _broll_credits(seconds: int, cogs_override: float = None) -> int:
    import video_providers as _vp
    return int(_vp.credits_for("text_to_video", SPK_BROLL_MODEL, seconds=seconds,
                               resolution=SPK_CLIP_RES, audio_on=False, cogs_override=cogs_override) or 0)


def _broll_worst_cogs() -> Optional[float]:
    """Worst-case (max known) per-second COGS across the b-roll model's chain — HOLD basis (option A)."""
    try:
        import pricing as _pricing
        return _pricing.bounds_any(SPK_BROLL_MODEL, "text_to_video").get("max_cost")
    except Exception:
        return None


def _wav_seconds(b: bytes) -> float:
    try:
        import io as _io, wave as _wave
        with _wave.open(_io.BytesIO(b), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 5.0


async def _concat_audio(parts: list) -> Optional[bytes]:
    """Concatenate per-segment WAV bytes into ONE continuous VO track (ffmpeg concat demuxer). The segment
    audios all come from the SAME TTS path → same codec → -c copy concatenates losslessly. The concatenated
    VO == the concatenated video timeline, so the per-segment lip-sync stays aligned. None → caller falls
    back to the first part."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    import tempfile, subprocess
    ff = os.getenv("FFMPEG_BIN", "ffmpeg")
    with tempfile.TemporaryDirectory() as d:
        listpath = os.path.join(d, "list.txt")
        with open(listpath, "w") as lf:
            for i, p in enumerate(parts):
                fp = os.path.join(d, f"a{i}.wav")
                with open(fp, "wb") as af:
                    af.write(p)
                lf.write(f"file '{fp}'\n")
        outp = os.path.join(d, "out.wav")
        try:
            await asyncio.to_thread(lambda: subprocess.run(
                [ff, "-y", "-f", "concat", "-safe", "0", "-i", listpath, "-c", "copy", outp],
                check=True, capture_output=True, timeout=90))
            with open(outp, "rb") as of:
                return of.read()
        except Exception as e:
            log.info("_concat_audio failed (%s) → first segment only", e)
            return parts[0]


async def _creep_progress(progress_fn, phase: str, lo: int, hi: int, label: str, est_secs: float = 80.0):
    """Creep the progress bar lo→hi over ~est_secs while ONE long step runs (the avatar render is a single
    `await dispatch` that otherwise freezes the bar). The caller cancels this when the step completes;
    it never exceeds hi, so the real next-step update stays monotonic. Best-effort — swallows errors."""
    span = max(1, int(hi) - int(lo))
    interval = max(1.5, float(est_secs) / span)
    pct = int(lo)
    try:
        while pct < int(hi):
            await asyncio.sleep(interval)
            pct += 1
            await progress_fn(phase, pct, label)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _render_segments(segments: list, portrait_url: str, voice: str, language: str, want_captions: bool,
                           tenant_id, user_id, op_id: str, byok: bool, _api, v: int, budget_seconds: float = 0.0):
    """Render each beat → a clip (presenter avatar | b-roll text_to_video), driven by its OWN VO slice.
    Returns (clips, seg_audios, captions, committed). Only SUCCESSFUL beats are kept so clips ↔ audios stay
    in sync (the concatenated VO then aligns with the concatenated video). Captions are SEGMENT-ACCURATE
    (Phase 3): each beat's text is windowed to its exact [t, t+dur). `budget_seconds`>0 stops starting NEW
    beats once the cumulative duration reaches it (margin guard: bounds COGS to ≈ the held estimate)."""
    import video_providers as _vp
    clips, seg_audios, captions, committed, t = [], [], [], 0, 0.0
    for i, seg in enumerate(segments[:SPK_MAX_SEGMENTS]):
        if budget_seconds and clips and t >= budget_seconds:
            log.info("spokesperson b-roll hit duration budget %.1fs at beat %d/%d → trim remaining",
                     budget_seconds, i, len(segments))
            break
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        audio = await _synth_vo(text, voice, language)
        if audio is None:
            continue
        dur = max(1.0, _wav_seconds(audio))
        role = (seg.get("role") or "presenter").lower()
        try:
            if role == "broll":
                brief = (seg.get("broll_brief") or seg.get("scene") or text)
                r = await _vp.dispatch("text_to_video", SPK_BROLL_MODEL,
                                       {"prompt": brief, "aspect": "9:16", "seconds": int(max(2, round(dur))),
                                        "resolution": SPK_CLIP_RES}, f"{op_id}-v{v}-broll{i}")
                ccost = _broll_credits(int(max(2, round(dur))), cogs_override=(r or {}).get("cost_usd"))
            else:
                aud_url = await _host_bytes(audio, "audio/wav", f"{op_id}-v{v}-seg{i}vo")
                if not aud_url:
                    continue
                r = await _vp.dispatch(SPK_AVATAR_FEATURE, SPK_AVATAR_MODEL,
                                       {"prompt": "a presenter speaking naturally to the camera",
                                        "resolution": SPK_CLIP_RES, "ref_images": [{"url": portrait_url}],
                                        "audio_ref": {"url": aud_url}}, f"{op_id}-v{v}-pres{i}")
                ccost = _avatar_credits_actual((r or {}).get("cost_usd"), int(max(1, round(dur))))
        except Exception as e:
            log.info("spokesperson segment %d render failed (%s) → skip", i, e)
            continue
        if r is None or r.get("data") is None:
            continue
        clips.append({"bytes": r["data"]})
        seg_audios.append(audio)
        committed += _commit_tts(text, tenant_id, user_id, op_id, byok, _api) + ccost
        if want_captions:
            # segment-accurate captions (Phase 3): chunk THIS beat's words into ~8-word windows
            # spread evenly across its exact [t, t+dur) slot, so captions read in lock-step with VO.
            cap_words = text.split()
            _cw = SPK_CAPTION_WORDS
            groups = [" ".join(cap_words[j:j + _cw]) for j in range(0, len(cap_words), _cw)] or [text]
            cspan = dur / len(groups)
            for gi, g in enumerate(groups):
                cs = t + gi * cspan
                captions.append({"t": round(cs, 2), "text": g, "end": round(cs + cspan, 2)})
        t += dur
    return clips, seg_audios, captions, committed


# ── script planner (Phase 1: produce the spoken script) ────────────────────────────
async def plan(input: dict) -> dict:
    """Return {"script"} (Phase 1) or {"script","segments":[{role,text,broll_brief}]} when b-roll is on
    (Phase 2). Best-effort: any failure → a minimal deterministic script (+ a single presenter segment when
    b-roll is on) so the render is never blocked."""
    seconds = _spk_seconds(input)
    vo = input.get("voiceover") or {}
    given = (vo.get("script") or "").strip()
    topic = (input.get("topic") or input.get("product_desc") or "").strip()
    language = (input.get("language") or "English").strip()
    cta = (input.get("cta") or "").strip()
    vibe = (input.get("vibe") or "friendly").lower()
    words = int(seconds * 2.2)

    if not _broll_on(input):
        if given:
            return {"script": given[:4000]}
        if not topic:
            return {"script": ""}
        prompt = (
            "You are a short-form video scriptwriter. Write ONLY the spoken words (no scene directions, no "
            "labels, no quotes) for a single on-camera presenter, in " + language + ", " + vibe + " tone, "
            "timed to about " + str(seconds) + " seconds of speech (~" + str(words) + " words). "
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

    # ── b-roll ON → script + interleaved presenter/b-roll shot plan (JSON) ──
    if not (given or topic):
        return {"script": "", "segments": []}
    basis = ("Base it on this exact script (keep the words): " + given) if given else ("Topic: " + topic)
    prompt = (
        "You are a short-form video director. Output STRICT JSON ONLY (no prose, no code fences):\n"
        '{"script":"<full spoken voiceover>","segments":[{"role":"presenter"|"broll","text":"<spoken words '
        'for THIS beat>","broll_brief":"<for broll only: what the cutaway visually shows>"}]}\n'
        "RULES: the segments' texts IN ORDER must read as the full script. Alternate a talking-head "
        "PRESENTER beat (hook, key points, the CTA) with short BROLL cutaways that visually illustrate the "
        "line being spoken. 4-8 segments, ~" + str(words) + " words total, " + language + ", " + vibe +
        " tone. " + basis + (" End on the CTA: '" + cta + "'." if cta else "") + "\nJSON only.")
    try:
        import laozhang_api as _api
        import json as _json
        cl = _api.make_client(RECIPE_PLAN_MODEL)
        mt = min(1300, _api.MODEL_MAX_TOKENS.get(_api.MODELS.get(RECIPE_PLAN_MODEL, RECIPE_PLAN_MODEL),
                                                 _api.DEFAULT_MAX_TOKENS))
        r = await asyncio.to_thread(lambda: cl.chat.completions.create(
            model=RECIPE_PLAN_MODEL, messages=[{"role": "user", "content": prompt}],
            temperature=0.7, max_tokens=mt))
        raw = (r.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            import re as _re
            raw = _re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = _re.sub(r"\n?```\s*$", "", raw).strip()
        parsed = _json.loads(raw)
        segs = [s for s in (parsed.get("segments") or [])
                if isinstance(s, dict) and (s.get("text") or "").strip()][:SPK_MAX_SEGMENTS]
        if segs:
            script = (parsed.get("script") or " ".join(s.get("text", "") for s in segs)).strip()
            return {"script": script[:4000], "segments": segs}
    except Exception as e:
        log.info("spokesperson b-roll plan failed (%s) → single-presenter fallback", e)
    fb = (given or (f"{topic}. {cta}".strip() if cta else topic))[:4000]
    return {"script": fb, "segments": [{"role": "presenter", "text": fb}]}


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

    if _broll_on(input):
        pres_s = max(1, int(round(seconds * SPK_PRESENTER_RATIO)))
        broll_s = max(1, seconds - pres_s)
        av = _avatar_credits_hold(pres_s)   # HOLD = worst-case provider (option A)
        if av:
            line_items.append({"label": f"Presenter · {SPK_AVATAR_MODEL} (~{pres_s}s × {n_variants})",
                               "credits": av * n_variants})
            total += av * n_variants
        bv = _broll_credits(broll_s, cogs_override=_broll_worst_cogs())
        if bv:
            line_items.append({"label": f"B-roll · {SPK_BROLL_MODEL} (~{broll_s}s × {n_variants})",
                               "credits": bv * n_variants})
            total += bv * n_variants
    else:
        av = _avatar_credits_hold(seconds)   # HOLD = worst-case provider (option A)
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
    cap_style = (input.get("captions_style") or "karaoke").strip().lower()  # spokesperson = karaoke by default
    if cap_style not in ("karaoke", "block"):
        cap_style = "karaoke"
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

            # 2. script (+ optional b-roll shot plan). plan() returns {"script"} or, when b-roll is
            #    on, {"script","segments":[…]} interleaving presenter beats with cutaways.
            await _progress("script", base_pct, f"Writing the script (variant {v + 1})…")
            plan_out = await plan(input)
            script = (plan_out.get("script") or "").strip()
            segments = plan_out.get("segments")

            if segments:
                # ── Phase 2/3: per-segment render. Each beat is driven by ITS OWN VO slice — a
                #    talking-head avatar for PRESENTER beats, a text_to_video cutaway for BROLL — so
                #    the clips concatenate to the full VO with segment-accurate captions. Any failed
                #    beat is dropped in lockstep (clip + its audio) to keep the timing exact.
                await _progress("render", base_pct + 8, f"Filming the segments (variant {v + 1})…")
                _tick = asyncio.create_task(_creep_progress(
                    _progress, "render", base_pct + 8, base_pct + 21,
                    f"Filming the segments (variant {v + 1})…", est_secs=120.0))
                try:
                    clips, seg_audios, captions, seg_committed = await _render_segments(
                        segments, portrait_url, voice, language, want_captions,
                        tenant_id, user_id, op_id, byok, _api, v, budget_seconds=seconds * SPK_DURATION_TOLERANCE)
                finally:
                    _tick.cancel()
                if not clips:
                    raise RuntimeError(f"variant {v + 1}: no segment rendered")
                committed_total += seg_committed
                vo_bytes = await _concat_audio(seg_audios)
                if vo_bytes is None:
                    raise RuntimeError(f"variant {v + 1}: voiceover concat failed")
            else:
                # ── Phase 1: a single talking-head clip driven by the whole VO. ──
                await _progress("voice", base_pct + 6, f"Recording the voiceover (variant {v + 1})…")
                vo_bytes = await _synth_vo(script, voice, language)
                if vo_bytes is None:
                    raise RuntimeError("voiceover synthesis failed (a spokesperson needs a voice)")
                committed_total += _commit_tts(script, tenant_id, user_id, op_id, byok, _api)
                vo_url = await _host_bytes(vo_bytes, "audio/wav", f"{op_id}-v{v}-vo")
                if not vo_url:
                    raise RuntimeError("voiceover hosting unavailable (R2 off)")
                await _progress("render", base_pct + 14, f"Filming the presenter (variant {v + 1})…")
                talk_prompt = "a presenter speaking naturally to the camera, subtle head and hand motion"
                # the avatar render is one long external call (~60-120s) — creep the bar so it doesn't
                # visibly freeze; cancel the instant the render returns.
                _tick = asyncio.create_task(_creep_progress(
                    _progress, "render", base_pct + 14, base_pct + 21,
                    f"Filming the presenter (variant {v + 1})…", est_secs=90.0))
                try:
                    res = await _vp.dispatch(SPK_AVATAR_FEATURE, SPK_AVATAR_MODEL,
                                             {"prompt": talk_prompt, "resolution": SPK_CLIP_RES,
                                              "ref_images": [{"url": portrait_url}], "audio_ref": {"url": vo_url}},
                                             f"{op_id}-v{v}-presenter")
                finally:
                    _tick.cancel()
                if res is None or res.get("data") is None:
                    raise RuntimeError(f"variant {v + 1}: presenter render failed")
                # COMMIT at the ACTUAL provider served (option A): the hold reserved worst-case; charge what
                # we really pay (dispatch.cost_usd) → fair, and the umbrella refunds the slack.
                committed_total += _avatar_credits_actual(res.get("cost_usd"), seconds)
                clips = [{"bytes": res["data"]}]
                # time captions to the ACTUAL VO length (not the requested seconds) so karaoke
                # word-fill lands on the real clip — the avatar clip == the VO duration. Short lines
                # (SPK_CAPTION_WORDS) so a vertical frame never overflows.
                vo_secs = _wav_seconds(vo_bytes)
                captions = _captions_from_script(script, vo_secs, chunk=SPK_CAPTION_WORDS) if (want_captions and script) else None

            # 3. music (optional) → ducked under the VO in assembly.
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

            # 4. assemble per aspect — re-mux the SAME master VO (concatenated for b-roll) so the
            #    lip-sync and cutaway timing stay exact; burn captions + brand. Phase 1 = one clip.
            for aspect in aspects:
                await _progress("assemble", base_pct + 22, f"Editing {aspect} (variant {v + 1})…")
                try:
                    mp4 = await _asm.assemble(clips, aspect, vo_audio=vo_bytes,
                                              music_bed=music_input, captions=captions, cta=cta, brand=brand,
                                              captions_style=cap_style)
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
    job_type_enum value ('veo' is used; the recipe identity lives in metadata). Returns the REAL R2
    KEY (signed on read by the job-result endpoint) — built the SAME way _persist_asset stored it, NOT
    a constructed path, or the signed URL 404s and the user is charged for an unretrievable video
    (the dispatch KEY-not-URL bug class). Best-effort → key | None."""
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
    # The R2 KEY for the signed-on-read URL: build the SAME key _persist_asset used (job_id=None).
    try:
        import storage
        if storage.is_configured():
            return storage.build_key(tenant_id, None, "video", fname)
    except Exception:
        pass
    return None

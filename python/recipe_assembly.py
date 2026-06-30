# ─────────────────────────────────────────────────────────────────────────────
# recipe_assembly.py — Product-Ad recipe: per-aspect ffmpeg assembly.
#
# Contract §2 (recipe_assembly.py). ONE public coroutine:
#
#   async assemble(clips, aspect, vo_audio=None, music_bed=None,
#                  captions=None, cta=None, brand=None) -> mp4_bytes
#
# It fuses the per-beat animated clips into a single, web-friendly H.264/AAC MP4
# for ONE target aspect (9:16 | 1:1 | 16:9). The pipeline, in order:
#
#   1. normalise each clip → exact W×H for the aspect (scale-to-cover + crop, or
#      scale-to-fit + letterbox/pad — chosen per `fit`), uniform fps / yuv420p.
#   2. concat the normalised clips — optional xfade crossfade at each seam
#      (matches the VI stitcher's xfade/acrossfade offset model in ffmpeg.mjs).
#   3. mux audio: VO over a DUCKED music bed (sidechaincompress so the bed dips
#      under narration), or whichever of the two is present; else silent track.
#   4. burn DETERMINISTIC overlays — captions (timed drawtext windows) and a CTA
#      end-card, plus an optional brand logo PNG. These are NEVER generative:
#      product/text/logo pixels are owned by us, drawn by ffmpeg, pixel-exact.
#
# SELF-CONTAINED & GUARDED: this module owns its own ffmpeg subprocess calls
# (the VI stitcher in backend/video/ffmpeg.mjs is Node and worker-gated; we do
# NOT call it). All ffmpeg work happens inside this module's tempdir and is
# cleaned up. If the ffmpeg binary is absent we raise a clear RuntimeError.
#
# Inputs are tolerant: a "clip" may be a local path (str), an in-memory mp4
# (bytes), or a {"path"|"bytes"|"url"} dict. URL clips are fetched SSRF-safely +
# size-capped by reusing image_providers' hardened fetch helpers when available.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Optional

# ── ffmpeg binaries (env-overridable, mirror the VI defaults) ─────────────────
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")

# ── render defaults (env-tunable; mirror VIDEO_DEFAULTS in ffmpeg.mjs) ─────────
FPS = int(os.getenv("RECIPE_FPS", os.getenv("VIDEO_FPS", "30")))
XFADE = float(os.getenv("RECIPE_XFADE", "0.4"))        # crossfade seconds between clips (0 → hard cut)
TRANSITION = os.getenv("RECIPE_TRANSITION", "fade")     # any ffmpeg xfade transition name
PRESET = os.getenv("RECIPE_PRESET", "veryfast")
CRF = int(os.getenv("RECIPE_CRF", "20"))
# CPU caps — bound BOTH the codec pool (-threads) and the SEPARATE filtergraph
# pool (-filter_complex_threads); the latter is why -threads alone doesn't cap CPU.
THREADS = int(os.getenv("RECIPE_THREADS", "2"))
FILTER_THREADS = int(os.getenv("RECIPE_FILTER_THREADS", "2"))
# Music ducking: how far the bed is pushed under the VO (sidechaincompress).
MUSIC_DUCK_RATIO = float(os.getenv("RECIPE_MUSIC_DUCK_RATIO", "8"))    # compression ratio under VO
MUSIC_BED_GAIN = float(os.getenv("RECIPE_MUSIC_BED_GAIN", "0.35"))     # bed level before ducking (0..1)
# Caption / CTA styling (drawtext, deterministic).
CAP_FONT_RATIO = float(os.getenv("RECIPE_CAP_FONT_RATIO", "0.045"))    # caption font px = ratio × frame H
CTA_FONT_RATIO = float(os.getenv("RECIPE_CTA_FONT_RATIO", "0.060"))    # CTA font px = ratio × frame H
CTA_SECONDS = float(os.getenv("RECIPE_CTA_SECONDS", "2.5"))            # end-card hold (clamped to total)
LOGO_WIDTH_RATIO = float(os.getenv("RECIPE_LOGO_WIDTH_RATIO", "0.18")) # logo width = ratio × frame W
TIMEOUT_S = int(os.getenv("RECIPE_FFMPEG_TIMEOUT", "600"))             # per-ffmpeg-invocation wall clock

# ── aspect → exact even WxH (1080-class short edge; provider-agnostic) ─────────
# Even dimensions required by yuv420p. Mirrors the standard pairs the video
# providers emit (1080×1920 / 1080×1080 / 1920×1080).
_ASPECT_DIMS = {
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "16:9": (1920, 1080),
}

_FONT_CANDIDATES = (
    # First existing wins. Debian worker image ships Liberation/DejaVu; macOS dev
    # boxes ship the system fonts. drawtext needs a real fontfile path.
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
)


# ══════════════════════════ ffmpeg availability ══════════════════════════
def _require_ffmpeg() -> tuple[str, str]:
    """Resolve the ffmpeg/ffprobe binaries or raise clearly. (The contract: if the
    ffmpeg binary is absent, raise a clear error — never silently degrade.)"""
    ff = shutil.which(FFMPEG) or (FFMPEG if os.path.isabs(FFMPEG) and os.path.exists(FFMPEG) else None)
    fp = shutil.which(FFPROBE) or (FFPROBE if os.path.isabs(FFPROBE) and os.path.exists(FFPROBE) else None)
    if not ff:
        raise RuntimeError(
            f"recipe_assembly: ffmpeg binary not found (looked for {FFMPEG!r}; set FFMPEG_BIN). "
            "Assembly requires ffmpeg installed in the worker image."
        )
    if not fp:
        # ffprobe drives the crossfade offsets; without it we degrade to hard-cut
        # concat (still correct) rather than failing the whole render.
        fp = ""
    return ff, fp


def _font_file() -> Optional[str]:
    env = os.getenv("RECIPE_CAPTION_FONTFILE")
    if env and os.path.exists(env):
        return env
    for c in _FONT_CANDIDATES:
        if os.path.exists(c):
            return c
    return None


# ══════════════════════════ subprocess plumbing ══════════════════════════
async def _run(args: list[str], cwd: str) -> None:
    """Run an ffmpeg argv off the event loop (subprocess is blocking). Raises with a
    stderr tail on non-zero exit. GUARDED to this module — the only place we shell
    out. Bounded by TIMEOUT_S so a wedged encode can't hang the job forever."""
    ff, _ = _require_ffmpeg()
    argv = [ff, *args]

    def _call() -> None:
        try:
            p = subprocess.run(
                argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"recipe_assembly: ffmpeg timed out after {TIMEOUT_S}s") from e
        if p.returncode != 0:
            tail = (p.stderr or b"").decode("utf-8", "replace")[-1500:]
            raise RuntimeError(f"recipe_assembly: ffmpeg exited {p.returncode}:\n{tail}")

    await asyncio.to_thread(_call)


async def _probe_duration(path: str) -> Optional[float]:
    """ffprobe a media file's duration (seconds) or None. Used to compute exact
    xfade offsets (the VI stitcher does the same with ffprobeDuration)."""
    _, fp = _require_ffmpeg()
    if not fp:
        return None

    def _call() -> Optional[float]:
        try:
            out = subprocess.run(
                [fp, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            ).stdout.decode("utf-8", "replace").strip()
            v = float(out)
            return v if v == v and v > 0 else None   # reject NaN / non-positive
        except Exception:
            return None

    return await asyncio.to_thread(_call)


_HAS_DRAWTEXT: Optional[bool] = None


async def _has_drawtext() -> bool:
    """Whether this ffmpeg build has the `drawtext` filter (needs libfreetype).
    Cached. The Debian worker image has it; some minimal/Homebrew builds don't —
    when absent we SKIP burned text (captions/CTA) rather than failing the whole
    render (mirrors the VI stitcher's hasSubtitlesFilter() graceful skip)."""
    global _HAS_DRAWTEXT
    if _HAS_DRAWTEXT is not None:
        return _HAS_DRAWTEXT
    ff, _ = _require_ffmpeg()

    def _call() -> bool:
        try:
            out = subprocess.run(
                [ff, "-hide_banner", "-filters"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            ).stdout.decode("utf-8", "replace")
            return any(line.split()[1:2] == ["drawtext"]
                       for line in out.splitlines() if line.strip())
        except Exception:
            return False

    _HAS_DRAWTEXT = await asyncio.to_thread(_call)
    return _HAS_DRAWTEXT


def _thread_args() -> list[str]:
    """-filter_complex_threads is a GLOBAL opt (goes up front); -threads is an
    OUTPUT opt (goes with the encoder). Omitted when 0 = ffmpeg default."""
    return (["-filter_complex_threads", str(FILTER_THREADS)] if FILTER_THREADS > 0 else [])


def _codec_thread_args() -> list[str]:
    return (["-threads", str(THREADS)] if THREADS > 0 else [])


def _enc_args(out: str) -> list[str]:
    """Common H.264/AAC web-friendly encode tail (+faststart)."""
    return [
        "-r", str(FPS),
        "-c:v", "libx264", *_codec_thread_args(), "-pix_fmt", "yuv420p",
        "-preset", PRESET, "-crf", str(CRF),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-max_muxing_queue_size", "9999", "-movflags", "+faststart",
        out,
    ]


# ══════════════════════════ input materialisation ══════════════════════════
async def _materialise_clip(clip: Any, dst: str) -> None:
    """Write a clip (path | bytes | {path|bytes|url}) to `dst`. URL clips are
    fetched SSRF-validated + size-capped via image_providers helpers when present."""
    if isinstance(clip, (bytes, bytearray)):
        _write(dst, bytes(clip)); return
    if isinstance(clip, str):
        if clip.startswith(("http://", "https://")):
            await _fetch_url(clip, dst); return
        if not os.path.exists(clip):
            raise RuntimeError(f"recipe_assembly: clip path not found: {clip}")
        shutil.copyfile(clip, dst); return
    if isinstance(clip, dict):
        if clip.get("bytes") is not None:
            b = clip["bytes"]
            _write(dst, bytes(b) if isinstance(b, (bytes, bytearray)) else _from_b64(b)); return
        if clip.get("b64"):
            _write(dst, _from_b64(clip["b64"])); return
        if clip.get("path"):
            return await _materialise_clip(clip["path"], dst)
        if clip.get("url"):
            await _fetch_url(clip["url"], dst); return
    raise RuntimeError(f"recipe_assembly: unsupported clip input type: {type(clip).__name__}")


def _from_b64(s: str) -> bytes:
    import base64
    # tolerate a data: URI prefix
    if isinstance(s, str) and s.startswith("data:"):
        s = s.split(",", 1)[-1]
    return base64.b64decode(s)


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


async def _fetch_url(url: str, dst: str) -> None:
    """Fetch a remote clip to dst. Prefer image_providers' hardened, SSRF-pinned,
    size-capped streaming fetch; fall back to a plain capped httpx stream so the
    module still works in isolation (tests / no-storage)."""
    try:
        import httpx  # local import — keeps module import cheap and dependency-soft
    except Exception as e:  # pragma: no cover
        raise RuntimeError("recipe_assembly: httpx required to fetch URL clips") from e
    data: Optional[bytes] = None
    try:
        import image_providers as _ip  # reuse the hardened SSRF-pinned capped GET
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            data, _mime = await _ip._capped_get(client, url, validate_public=True)
    except Exception:
        data = None
    if data is None:
        # self-contained fallback: capped stream (default 32MB) with no SSRF pin
        cap = int(os.getenv("RECIPE_MAX_CLIP_BYTES", str(64 * 1024 * 1024)))
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                if int(r.headers.get("content-length") or 0) > cap:
                    raise RuntimeError("recipe_assembly: clip too large")
                buf, total = bytearray(), 0
                async for chunk in r.aiter_bytes():
                    total += len(chunk)
                    if total > cap:
                        raise RuntimeError("recipe_assembly: clip too large")
                    buf += chunk
                data = bytes(buf)
    _write(dst, data)


# ══════════════════════════ filter builders ══════════════════════════
def _fit_filter(w: int, h: int, fit: str) -> str:
    """Per-clip geometry to EXACTLY w×h. `cover` scales up + center-crops (fills
    the frame, may crop edges); `pad` scales to fit + letterboxes (no crop). Both
    end uniform fps / yuv420p / sar 1 so clips concat/xfade cleanly. Mirrors the
    VI stitcher's `fit` (increase+crop) and `box` (decrease+pad) chains."""
    if fit == "pad":
        geo = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black")
    else:  # cover (default)
        geo = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
               f"crop={w}:{h}")
    return f"{geo},fps={FPS},setsar=1,format=yuv420p"


def _xfade_offsets(durs: list[float], xf: float) -> list[float]:
    """Running xfade offsets (length n-1): joining clip k starts its transition at
    (combined-so-far − xf). Identical model to xfadeOffsets in ffmpeg.mjs."""
    offs: list[float] = []
    combined = durs[0] if durs else 0.0
    for k in range(1, len(durs)):
        offs.append(round(combined - xf, 3))
        combined = round(combined + durs[k] - xf, 3)
    return offs


def _esc_drawtext(text: str) -> str:
    """Escape a literal string for ffmpeg drawtext's `text=` (NOT textfile). Order
    matters: backslash first. We also neutralise chars that break the filter parse
    (':' , '[' , ']' , ',' , ';' , '%')."""
    s = str(text or "")
    s = s.replace("\\", "\\\\").replace("'", "’")   # curly-quote apostrophes (avoid quote-soup)
    s = s.replace(":", "\\:").replace("%", "\\%")
    s = s.replace("[", "(").replace("]", ")")
    s = s.replace(",", "\\,").replace(";", "\\;")
    s = s.replace("\n", " ")
    return s


def _hex_to_ff(color: Optional[str], default: str) -> str:
    """Normalise a #RRGGBB (brand color) to ffmpeg's 0xRRGGBB; fall back to default."""
    if not color:
        return default
    c = str(color).strip().lstrip("#")
    if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
        return f"0x{c.upper()}"
    return default


def _drawtext_caption(text: str, start: float, end: float, w: int, h: int,
                      fontfile: Optional[str]) -> str:
    """A single timed, bottom-centred caption window (deterministic overlay)."""
    fs = max(12, int(round(h * CAP_FONT_RATIO)))
    box_y = f"h-{int(round(h * 0.16))}"
    ff = f"fontfile='{fontfile}':" if fontfile else ""
    return (
        f"drawtext={ff}text='{_esc_drawtext(text)}':"
        f"fontsize={fs}:fontcolor=white:borderw={max(2, fs // 11)}:bordercolor=black@0.9:"
        f"box=1:boxcolor=black@0.45:boxborderw={max(6, fs // 4)}:"
        f"x=(w-text_w)/2:y={box_y}:"
        f"enable='between(t,{start:.3f},{end:.3f})'"
    )


def _drawtext_cta(text: str, total: float, w: int, h: int, fontfile: Optional[str],
                  color: str) -> str:
    """A CTA end-card held for the last CTA_SECONDS (deterministic overlay)."""
    fs = max(16, int(round(h * CTA_FONT_RATIO)))
    hold = min(CTA_SECONDS, max(0.8, total * 0.4))
    start = max(0.0, total - hold)
    ff = f"fontfile='{fontfile}':" if fontfile else ""
    return (
        f"drawtext={ff}text='{_esc_drawtext(text)}':"
        f"fontsize={fs}:fontcolor=white:borderw={max(2, fs // 10)}:bordercolor=black@0.9:"
        f"box=1:boxcolor={color}@0.85:boxborderw={max(10, fs // 3)}:"
        f"x=(w-text_w)/2:y=(h-text_h)/2:"
        f"enable='gte(t,{start:.3f})'"
    )


# ══════════════════════════ the public entry point ══════════════════════════
async def assemble(
    clips: list[Any],
    aspect: str,
    vo_audio: Optional[Any] = None,
    music_bed: Optional[Any] = None,
    captions: Optional[list[dict]] = None,
    cta: Optional[str] = None,
    brand: Optional[dict] = None,
) -> bytes:
    """Assemble per-beat clips → ONE mp4 (bytes) for `aspect`.

    clips      : list of clip inputs (path str | bytes | {path|bytes|b64|url}); ≥1.
    aspect     : "9:16" | "1:1" | "16:9".
    vo_audio   : optional voiceover audio (same input shapes) — the master narration.
    music_bed  : optional music audio (same shapes) — DUCKED under the VO.
    captions   : optional [{"t": start_sec, "text": str, "end"?: sec}] — burned via
                 drawtext (deterministic; NEVER generative). Window k ends at the
                 next caption's start (or `end`/total) if not given.
    cta        : optional call-to-action string — burned as an end-card.
    brand      : optional {"logo_b64"|"logo_path"|"logo_url"?, "colors"?:[hex], "name"?}.

    Returns the encoded mp4 as bytes. Raises RuntimeError if ffmpeg is unavailable
    or no clips are given. All work happens in a private tempdir, cleaned up.
    """
    if not clips:
        raise RuntimeError("recipe_assembly.assemble: no clips")
    _require_ffmpeg()  # fail fast & clearly before doing any I/O
    aspect = aspect if aspect in _ASPECT_DIMS else "9:16"
    W, H = _ASPECT_DIMS[aspect]
    fit = "cover"  # product clips already framed; cover fills without bars. Use pad via env if needed.
    if os.getenv("RECIPE_FIT") == "pad":
        fit = "pad"
    fontfile = _font_file()
    colors = (brand or {}).get("colors") or []
    cta_color = _hex_to_ff(colors[0] if colors else None, "0x111111")

    work = tempfile.mkdtemp(prefix="recipe_asm_")
    try:
        # ── 1. materialise + normalise each clip to exact W×H ──────────────────
        norm_paths: list[str] = []
        norm_durs: list[float] = []
        for i, clip in enumerate(clips):
            raw = os.path.join(work, f"raw_{i}.mp4")
            await _materialise_clip(clip, raw)
            norm = os.path.join(work, f"norm_{i}.mp4")
            # normalise video to W×H AND attach a single, uniform SILENT stereo track
            # so every clip has matching streams for concat/xfade. The real audio bed
            # (VO + music) is muxed later — a beat clip's own audio is intentionally
            # discarded (the ad's soundtrack is deterministic, not per-clip). anullsrc
            # is looped and -shortest trims it to the clip's real video length.
            await _run([
                "-y", *_thread_args(),
                "-i", raw,
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-filter_complex",
                f"[0:v]{_fit_filter(W, H, fit)}[v]",
                "-map", "[v]",
                "-map", "1:a",
                "-shortest",
                *_enc_args(norm),
            ], cwd=work)
            d = await _probe_duration(norm)
            norm_paths.append(norm)
            norm_durs.append(d if d else 2.0)

        # ── 2. concat (optional xfade crossfade) → silent base video ───────────
        base = os.path.join(work, "base.mp4")
        await _concat(norm_paths, norm_durs, base, work, W, H)
        total = await _probe_duration(base) or sum(norm_durs)

        # ── 3. mux audio (VO over ducked music) ────────────────────────────────
        vo_path = await _opt_audio(vo_audio, work, "vo")
        music_path = await _opt_audio(music_bed, work, "music")
        muxed = os.path.join(work, "muxed.mp4")
        await _mux_audio(base, vo_path, music_path, total, muxed, work)

        # ── 4. burn deterministic overlays (captions + CTA + logo) ─────────────
        logo_path = await _opt_logo(brand, work)
        final = os.path.join(work, "final.mp4")
        await _overlay(muxed, total, W, H, captions, cta, cta_color, logo_path,
                       fontfile, final, work)

        with open(final, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ══════════════════════════ stage helpers ══════════════════════════
async def _concat(paths: list[str], durs: list[float], out: str, cwd: str,
                  w: int, h: int) -> None:
    """Concat normalised clips. With XFADE>0 and >1 clip: chain xfade(video)+
    acrossfade(audio) at running offsets (smooth seams, VI model). Else: concat
    demuxer byte-join of the already-uniform clips (no re-encode = cheap)."""
    n = len(paths)
    if n == 1:
        shutil.copyfile(paths[0], out)
        return
    xf = XFADE
    if xf > 0.05:
        # never let the crossfade exceed the shortest clip (negative offset = ffmpeg
        # rejects the graph). Same clamp the VI stitcher applies.
        xf = max(0.05, min(xf, min(durs) * 0.8))
        offs = _xfade_offsets(durs, xf)
        args = ["-y", *_thread_args()]
        for p in paths:
            args += ["-i", p]
        parts: list[str] = []
        vlab, alab = "0:v", "0:a"
        for k in range(1, n):
            vout = "vout" if k == n - 1 else f"vx{k}"
            aout = "aout" if k == n - 1 else f"ax{k}"
            parts.append(
                f"[{vlab}][{k}:v]xfade=transition={TRANSITION}:duration={xf:.3f}:"
                f"offset={offs[k - 1]:.3f}[{vout}]"
            )
            parts.append(f"[{alab}][{k}:a]acrossfade=d={xf:.3f}:c1=tri:c2=tri[{aout}]")
            vlab, alab = vout, aout
        args += ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]"]
        args += _enc_args(out)
        await _run(args, cwd=cwd)
        return
    # hard-cut byte concat — clips are already uniform W×H/fps/codec.
    listfile = os.path.join(cwd, "concat.txt")
    with open(listfile, "w") as f:
        for p in paths:
            safe = os.path.basename(p).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    await _run(["-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
                "-c", "copy", "-movflags", "+faststart", out], cwd=cwd)


async def _opt_audio(src: Any, cwd: str, tag: str) -> Optional[str]:
    """Materialise an optional audio input to a file, or None."""
    if src is None:
        return None
    dst = os.path.join(cwd, f"{tag}.audio")
    await _materialise_clip(src, dst)   # tolerant of bytes/path/url/dict (same shapes)
    return dst


async def _mux_audio(base: str, vo: Optional[str], music: Optional[str],
                     total: float, out: str, cwd: str) -> None:
    """Mux the audio bed onto the silent-ish base video.
      • VO only        → VO as the track.
      • music only     → music (bed gain) looped/trimmed to total.
      • VO + music      → music DUCKED under VO via sidechaincompress, then summed.
      • neither         → keep the base's existing track (silence) — passthrough copy.
    All audio is trimmed/padded to `total` so it locks to the video length."""
    if vo is None and music is None:
        shutil.copyfile(base, out)
        return

    args = ["-y", *_thread_args(), "-i", base]
    idx = 1
    vo_i = music_i = None
    if vo is not None:
        args += ["-i", vo]; vo_i = idx; idx += 1
    if music is not None:
        # loop the bed so a short track still covers the whole ad
        args += ["-stream_loop", "-1", "-i", music]; music_i = idx; idx += 1

    fc: list[str] = []
    if vo_i is not None and music_i is not None:
        # VO normalised; music gained down then SIDECHAIN-ducked by the VO envelope.
        fc.append(f"[{vo_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"apad,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[vo]")
        fc.append(f"[{music_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"volume={MUSIC_BED_GAIN:.3f},atrim=0:{total:.3f},asetpts=PTS-STARTPTS[bed]")
        # split VO: one copy is the sidechain key, one is the mix signal
        fc.append("[vo]asplit=2[vokey][vomix]")
        fc.append(f"[bed][vokey]sidechaincompress=threshold=0.03:ratio={MUSIC_DUCK_RATIO:.1f}:"
                  f"attack=20:release=300[duck]")
        fc.append("[vomix][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
    elif vo_i is not None:
        fc.append(f"[{vo_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"apad,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[aout]")
    else:  # music only
        fc.append(f"[{music_i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                  f"volume={MUSIC_BED_GAIN:.3f},atrim=0:{total:.3f},asetpts=PTS-STARTPTS[aout]")

    args += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[aout]"]
    args += _enc_args(out)
    await _run(args, cwd=cwd)


async def _opt_logo(brand: Optional[dict], cwd: str) -> Optional[str]:
    """Materialise an optional brand logo PNG to a file, or None."""
    if not brand:
        return None
    src: Any = None
    if brand.get("logo_b64"):
        src = {"b64": brand["logo_b64"]}
    elif brand.get("logo_path"):
        src = brand["logo_path"]
    elif brand.get("logo_url"):
        src = {"url": brand["logo_url"]}
    if src is None:
        return None
    dst = os.path.join(cwd, "logo.png")
    try:
        await _materialise_clip(src, dst)
    except Exception:
        return None   # logo is best-effort — never fail the whole render on a bad logo
    return dst


def _caption_windows(captions: list[dict], total: float) -> list[tuple[float, float, str]]:
    """Normalise [{t,text,end?}] into non-overlapping (start,end,text) windows.
    A window with no explicit end runs to the next caption's start (or total)."""
    items = []
    for c in captions or []:
        try:
            t = float(c.get("t", 0))
        except Exception:
            t = 0.0
        items.append((max(0.0, t), c.get("end"), str(c.get("text") or "")))
    items.sort(key=lambda x: x[0])
    out: list[tuple[float, float, str]] = []
    for i, (t, end, text) in enumerate(items):
        if not text.strip():
            continue
        if end is not None:
            try:
                e = float(end)
            except Exception:
                e = None
        else:
            e = None
        if e is None:
            e = items[i + 1][0] if i + 1 < len(items) else total
        e = min(max(e, t + 0.3), total)
        out.append((round(t, 3), round(e, 3), text))
    return out


async def _overlay(src: str, total: float, w: int, h: int,
                   captions: Optional[list[dict]], cta: Optional[str], cta_color: str,
                   logo: Optional[str], fontfile: Optional[str], out: str,
                   cwd: str) -> None:
    """Burn the DETERMINISTIC overlays: optional logo (top-right), timed captions,
    CTA end-card. If nothing to overlay, passthrough-copy. NEVER generative.

    Text overlays (captions/CTA) need the `drawtext` filter; when the ffmpeg build
    lacks it we skip the text but still apply the logo (overlay is always present)
    rather than failing the render."""
    cap_wins = _caption_windows(captions or [], total)
    want_text = bool(cap_wins) or bool(cta and cta.strip())
    can_text = want_text and await _has_drawtext()

    if not logo and not can_text:
        # nothing renderable (no logo, and no/unsupported text) → passthrough
        shutil.copyfile(src, out)
        return

    args = ["-y", *_thread_args(), "-i", src]
    fc: list[str] = []
    vlab = "0:v"

    if logo:
        args += ["-i", logo]
        lw = max(24, int(round(w * LOGO_WIDTH_RATIO)))
        margin = int(round(w * 0.04))
        fc.append(f"[1:v]scale={lw}:-1[logo]")
        fc.append(f"[{vlab}][logo]overlay=W-w-{margin}:{margin}[vlogo]")
        vlab = "vlogo"

    if can_text:
        draws: list[str] = []
        for (s, e, text) in cap_wins:
            draws.append(_drawtext_caption(text, s, e, w, h, fontfile))
        if cta and cta.strip():
            draws.append(_drawtext_cta(cta.strip(), total, w, h, fontfile, cta_color))
        if draws:
            fc.append(f"[{vlab}]" + ",".join(draws) + "[vtxt]")
            vlab = "vtxt"

    # -map must reference a LABEL when it came from the filtergraph, but a bare
    # input specifier (0:v) when no filter ran. fc is always non-empty here
    # (logo or text produced at least one stage).
    args += ["-filter_complex", ";".join(fc), "-map", f"[{vlab}]", "-map", "0:a?"]
    args += _enc_args(out)
    await _run(args, cwd=cwd)

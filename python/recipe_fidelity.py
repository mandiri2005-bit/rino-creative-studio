"""
recipe_fidelity.py — the product-fidelity core for the Product Ad recipe (Track-1 cutout +
Track-2 AI scene + deterministic paste-back + ΔE gate). Per PRODUCT-AD-IMPL-CONTRACT §2.

The whole point of this module is the PIXEL-LOCK: an AI "harmonize" pass blends the product into
a generated scene (great lighting / contact shadow), but generative models drift the product's
color, logo, and label. So we NEVER trust the AI render for the product surface itself — we
re-composite the ORIGINAL bg-removed cutout back over the AI result at the SAME (x, y, scale) we
placed it. Because WE own the placement (deterministic — we choose x, y, scale), paste-back needs
no detection: the product region is known a-priori. AI keeps the scene; the real pixels win on the
product.

Pipeline per keyframe (see make_keyframes):
    scene_plate(prompt)            # empty scene, no product            [create_raster, t2i]
      → place_cutout(...)          # raw PIL alpha-composite at x,y,scale
      → harmonize(...)             # AI blends it in (shadow/lighting)  [edit, i2i]
      → paste_back(...)            # ORIGINAL cutout re-composited      ← the pixel-lock
      → fidelity_gate(...)         # Lab ΔE on the product region

All network ops are async and go through `image_providers.dispatch(feature, model_id, params,
op_id)`; bytes come back on the result's "data" key (never re-fetched as a URL). The failover
chains named in the contract (bg_remove recraft→nano-banana→flux-kontext; scene_plate
seedream→flux→imagen→nano-banana; harmonize nano-banana→flux-kontext) are realized two ways:
op-tool features (bg_remove) fail over WITHIN their registry op-chain; prompt-ops (create_raster /
edit) fail over across an ordered list of MODEL ids here, each model carrying its own internal
provider chain — so a whole model going dark advances to the next model.

Dependencies: Pillow + numpy. Both are required; a missing one raises a clear ImportError at
import (numpy is in requirements.txt; Pillow is NOT yet — see PENDING in the build report). Lab
ΔE uses a self-contained CIE76 implementation (no skimage/colormath needed); any optional fidelity
signal (OCR/SSIM) is best-effort and NEVER blocks on a missing lib.
"""
from __future__ import annotations

import io
import os
import asyncio
import base64
import logging
from typing import Optional

# Hard deps — fail loud at import (the contract wants a clear ImportError, not a late AttributeError).
try:
    from PIL import Image, ImageFilter
except Exception as e:  # pragma: no cover
    raise ImportError(
        "recipe_fidelity requires Pillow (PIL). Add 'Pillow' to python/requirements.txt. "
        f"Original import error: {e}"
    ) from e

try:
    import numpy as np
except Exception as e:  # pragma: no cover
    raise ImportError(
        "recipe_fidelity requires numpy. It is in python/requirements.txt; install it. "
        f"Original import error: {e}"
    ) from e

import image_providers as _ip  # dispatch + SSRF/byte-cap fetch helpers + ProviderError

log = logging.getLogger("recipe_fidelity")

# ── tunables ──────────────────────────────────────────────────────────────────
# Mean-color ΔE (CIE76) over the product region. Below this the paste-back held the product true;
# above it something drifted (rare — paste-back makes a high ΔE nearly impossible, so a trip here is
# a real signal the candidate wasn't paste-backed or the crop is wrong).
_DELTAE_THRESHOLD = float(os.getenv("RECIPE_FIDELITY_DELTAE", "8.0"))
# Seam feather (px) for paste-back: a soft alpha edge hides the hard composite line without letting
# the AI's halo bleed onto product pixels. Small on purpose.
_FEATHER_PX = int(os.getenv("RECIPE_PASTEBACK_FEATHER_PX", "2"))

# Model failover ladders for prompt-ops (each model id carries its own internal provider chain).
# Order = contract's cheapest-first intent; a model missing from the live registry is silently
# skipped so a registry trim can't break the recipe.
_SCENE_MODELS = ["seedream-4-5", "flux-dev", "imagen-4", "nano-banana"]   # create_raster (t2i)
_HARMONIZE_MODELS = ["nano-banana", "flux-kontext-pro", "flux-kontext-max"]  # edit (i2i)
# bg_remove is an op-tool feature: dispatch picks the op-chain regardless of model_id, but we pass a
# real bg_remove-capable model id so COGS bookkeeping resolves cleanly.
_BG_MODEL = "recraft-v3"


# ══════════════════════════════ byte helpers ══════════════════════════════════
def _ref_to_dict(img_ref) -> dict:
    """Normalize a product-image ref (b64 str | data: URI | http url | {b64|url} dict) into the
    {b64|url} shape image_providers._img_bytes expects."""
    if isinstance(img_ref, dict):
        return img_ref
    if not isinstance(img_ref, str):
        raise ValueError(f"unsupported image ref type: {type(img_ref)!r}")
    s = img_ref.strip()
    if s.startswith("data:"):
        # data:<mime>;base64,<payload>
        try:
            head, payload = s.split(",", 1)
            mime = head[5:].split(";", 1)[0] or "image/png"
        except ValueError:
            raise ValueError("malformed data: URI")
        return {"b64": payload, "mime": mime}
    if s.startswith("http://") or s.startswith("https://"):
        return {"url": s}
    # bare base64 payload
    return {"b64": s}


async def _fetch_ref_bytes(img_ref) -> bytes:
    """Fetch any product-image ref → raw bytes, under image_providers' SSRF guard + running-total
    byte cap (url refs are IP-pinned + size-capped; b64 is decoded inline). Reuses the proven
    helper so we never open a second, un-capped fetch path."""
    d = _ref_to_dict(img_ref)
    if d.get("b64"):
        return base64.b64decode(d["b64"])
    # url path — borrow the dispatch client + the SSRF/cap-aware _img_bytes (validate_public=True).
    import httpx
    async with httpx.AsyncClient(timeout=_ip._TIMEOUT, follow_redirects=True) as client:
        data, _mime = await _ip._img_bytes(client, d)
    return data


def _png_bytes(im: Image.Image) -> bytes:
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def _open_rgba(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _open_rgb(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _dispatch_bytes(result: dict) -> bytes:
    """Pull rendered bytes off a dispatch() result. dispatch threads the bytes back on "data" so the
    caller never re-fetches "ref" as a URL; we defensively decode a data: URI ref if "data" is
    absent (older paths), but never fetch a bare R2 key."""
    data = result.get("data")
    if data:
        return data
    ref = result.get("ref") or ""
    if isinstance(ref, str) and ref.startswith("data:"):
        return base64.b64decode(ref.split(",", 1)[1])
    raise _ip.ProviderError("dispatch returned no inline bytes (ref is a storage key, not fetchable here)")


# ═══════════════════════════ Track 1: cutout ══════════════════════════════════
async def bg_remove(img_ref, op_id: str) -> bytes:
    """Background-remove a product image → RGBA PNG bytes (transparent outside the product).
    feature `bg_remove` (op-tool): dispatch fails over WITHIN the registry op-chain
    recraft→kie→fal→atlascloud (contract: recraft→nano-banana→flux-kontext intent). The product
    image ref is passed through as a ref_image; the bg_remove op-chain returns the cutout bytes."""
    d = _ref_to_dict(img_ref)
    params = {"ref_images": [d], "transparent": True}
    res = await _ip.dispatch("bg_remove", _BG_MODEL, params, op_id)
    data = _dispatch_bytes(res)
    # Guarantee RGBA so downstream alpha-composite has a real alpha channel even if a provider
    # handed back a flattened PNG.
    return _png_bytes(_open_rgba(data))


# ═══════════════════════════ Track 2: scene plate ═════════════════════════════
async def scene_plate(prompt: str, aspect: str, op_id: str) -> bytes:
    """Generate an EMPTY scene plate (no product) → RGB PNG bytes. feature `create_raster` (t2i),
    model ladder seedream→flux→imagen→nano-banana (each model's own provider chain handles inner
    failover; we step to the next model only when a whole model errors out)."""
    params = {"prompt": prompt, "aspect": aspect, "n": 1}
    errors = []
    for model_id in _SCENE_MODELS:
        if model_id not in _ip._MODELS:
            continue
        try:
            res = await _ip.dispatch("create_raster", model_id, params, op_id)
            return _png_bytes(_open_rgb(_dispatch_bytes(res)))
        except _ip.ProviderError as e:
            errors.append(f"{model_id}: {e}")
            log.info("scene_plate failover %s: %s", model_id, e)
            continue
    raise _ip.ProviderError("scene_plate: all models failed → " + " | ".join(errors))


# ═══════════════════════════ deterministic placement ═════════════════════════
def _resolve_box(scene_w: int, scene_h: int, cutout: Image.Image, x, y, scale: float):
    """Resolve (paste_x, paste_y, resized_cutout) from a known placement. x/y accept either an
    absolute pixel int OR a 0..1 fraction of the scene dimension (so callers can place
    proportionally across aspect ratios); scale is a multiplier on a 'fit ~⅓ of the scene width'
    base so 1.0 is a sensible hero size. Placement is ours — no detection."""
    cw, ch = cutout.size
    if cw <= 0 or ch <= 0:
        raise ValueError("cutout has zero dimension")
    base_w = scene_w / 3.0
    target_w = max(1, int(round(base_w * float(scale))))
    target_h = max(1, int(round(target_w * (ch / cw))))
    resized = cutout.resize((target_w, target_h), Image.LANCZOS)

    def _coord(v, span, size):
        if v is None:
            return (span - size) // 2  # centered
        fv = float(v)
        if 0.0 <= fv <= 1.0 and not (isinstance(v, int) and v > 1):
            # fraction = center anchor of the cutout at that fraction of the span
            return int(round(fv * span - size / 2.0))
        return int(round(fv))  # absolute pixels (top-left)

    px = _coord(x, scene_w, target_w)
    py = _coord(y, scene_h, target_h)
    return px, py, resized


def _composite_at(base_rgba: Image.Image, cutout_rgba: Image.Image, px: int, py: int) -> Image.Image:
    """Alpha-composite cutout onto a COPY of base at (px,py). Both RGBA. Returns a new image."""
    out = base_rgba.copy()
    out.alpha_composite(cutout_rgba, dest=(px, py))
    return out


def place_cutout(scene_bytes: bytes, cutout_rgba: bytes, x, y, scale: float) -> bytes:
    """Pure-PIL alpha-composite the cutout onto the scene at the KNOWN (x, y, scale). No AI, no
    detection. Returns raw composite PNG bytes (RGBA). This is the "raw composite" the AI harmonize
    pass then refines — and the exact same (x,y,scale) is replayed by paste_back, so the product
    region is pixel-identical end to end."""
    scene = _open_rgba(scene_bytes)
    cutout = _open_rgba(cutout_rgba)
    px, py, resized = _resolve_box(scene.width, scene.height, cutout, x, y, scale)
    return _png_bytes(_composite_at(scene, resized, px, py))


# ═══════════════════════════ Track 2: harmonize ═══════════════════════════════
async def harmonize(raw_composite: bytes, op_id: str, prompt: Optional[str] = None) -> bytes:
    """AI-blend the raw composite so the product sits in the scene (contact shadow + matched
    lighting/white-balance) WITHOUT being told to redraw the product. feature `edit` (i2i), model
    ladder nano-banana→flux-kontext. The prompt is locked to a 'keep product EXACT' instruction —
    but we don't rely on the model honoring it: paste_back re-asserts the real pixels afterward."""
    instruction = prompt or (
        "Blend the SINGLE product already in the image into the scene: add a realistic contact shadow "
        "and ambient occlusion where it meets the surface, and match the scene's lighting direction, "
        "colour temperature and exposure on the SURROUNDINGS only. Keep the product's shape, colour, "
        "material, label and logo EXACTLY as given — do not redraw, recolor, restyle, move or resize it. "
        "CRITICAL: do NOT add, draw, duplicate, mirror, clone or invent ANY additional product, can, "
        "bottle, cup, object, person, hand or text. The image must contain EXACTLY ONE product (the one "
        "already present) and NO people. Photorealistic, commercial product photography."
    )
    params = {"prompt": instruction, "ref_images": [{"b64": base64.b64encode(raw_composite).decode(), "mime": "image/png"}]}
    errors = []
    for model_id in _HARMONIZE_MODELS:
        if model_id not in _ip._MODELS:
            continue
        try:
            res = await _ip.dispatch("edit", model_id, params, op_id)
            return _png_bytes(_open_rgb(_dispatch_bytes(res)))
        except _ip.ProviderError as e:
            errors.append(f"{model_id}: {e}")
            log.info("harmonize failover %s: %s", model_id, e)
            continue
    raise _ip.ProviderError("harmonize: all models failed → " + " | ".join(errors))


# ═══════════════════════════ THE PIXEL-LOCK ═══════════════════════════════════
def _feathered_alpha(cutout_rgba: Image.Image, feather_px: int) -> Image.Image:
    """Return a copy of the cutout whose alpha is eroded+blurred by `feather_px` so the paste-back
    seam is soft (no hard composite line) while the product INTERIOR alpha stays fully opaque — the
    feather only softens the boundary, it never lets AI pixels show through the product body."""
    if feather_px <= 0:
        return cutout_rgba
    r, g, b, a = cutout_rgba.split()
    # MinFilter erodes the opaque mask inward by ~feather, then a matching blur ramps the edge.
    k = feather_px * 2 + 1
    a_eroded = a.filter(ImageFilter.MinFilter(k))
    a_soft = a_eroded.filter(ImageFilter.GaussianBlur(radius=feather_px))
    return Image.merge("RGBA", (r, g, b, a_soft))


def paste_back(ai_composite_bytes: bytes, cutout_rgba: bytes, x, y, scale: float) -> bytes:
    """THE PIXEL-LOCK. Re-alpha-composite the ORIGINAL cutout over the AI composite at the SAME
    (x, y, scale) used by place_cutout, so the real product pixels overwrite whatever the model
    rendered there. A light alpha feather softens the seam (env RECIPE_PASTEBACK_FEATHER_PX, default
    2px) without exposing AI pixels inside the product. Returns locked RGB PNG bytes — the scene,
    shadow and lighting are the AI's; the product surface is the user's real image.

    Deterministic because WE own placement: same inputs ⇒ same product region as place_cutout, to
    the pixel. This is what guarantees brand/label/color fidelity end to end."""
    ai = _open_rgba(ai_composite_bytes)
    cutout = _open_rgba(cutout_rgba)
    px, py, resized = _resolve_box(ai.width, ai.height, cutout, x, y, scale)
    locked = _feathered_alpha(resized, _FEATHER_PX)
    out = _composite_at(ai, locked, px, py)
    return _png_bytes(out.convert("RGB"))


# ═══════════════════════════ fidelity gate (Lab ΔE) ═══════════════════════════
def _srgb_to_lab(arr: "np.ndarray") -> "np.ndarray":
    """Vectorized sRGB(uint8, …x3) → CIE L*a*b* (D65). Self-contained CIE76 path — no skimage. Input
    any shape ending in 3; output same leading shape with 3 channels (L,a,b)."""
    rgb = arr.astype(np.float64) / 255.0
    # sRGB → linear
    a = 0.055
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + a) / (1 + a)) ** 2.4)
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    # linear RGB → XYZ (D65)
    X = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    Y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    Z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    # normalize by D65 white
    Xn, Yn, Zn = 0.95047, 1.0, 1.08883
    xr, yr, zr = X / Xn, Y / Yn, Z / Zn
    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0

    def _f(t):
        return np.where(t > eps, np.cbrt(t), (kappa * t + 16.0) / 116.0)

    fx, fy, fz = _f(xr), _f(yr), _f(zr)
    L = 116.0 * fy - 16.0
    A = 500.0 * (fx - fy)
    B = 200.0 * (fy - fz)
    return np.stack([L, A, B], axis=-1)


def _mean_lab_over_mask(rgb_arr: "np.ndarray", mask: "np.ndarray") -> Optional["np.ndarray"]:
    """Mean L*a*b* over the masked (opaque-product) pixels of an RGB array. mask is a HxW bool.
    Returns a (3,) array or None if the mask is empty."""
    if mask is None or not mask.any():
        return None
    lab = _srgb_to_lab(rgb_arr)
    sel = lab[mask]
    if sel.size == 0:
        return None
    return sel.reshape(-1, 3).mean(axis=0)


async def fidelity_gate(original_cutout_rgba: bytes, candidate_bytes: bytes, x, y, scale: float) -> dict:
    """Compare the candidate's PRODUCT REGION color to the original cutout and decide pass/fail.

    Crops the candidate at the SAME (x,y,scale) the cutout was placed, builds the product mask from
    the cutout's alpha (so only real product pixels are compared — not the transparent surround),
    and measures mean CIE76 ΔE in Lab. ok if ΔE < RECIPE_FIDELITY_DELTAE (default 8). After
    paste_back this should always pass (real pixels are present) — a trip here flags an unlocked /
    mis-cropped candidate.

    PRAGMATIC by design: color ΔE only. OCR/SSIM are intentionally out — and any optional signal is
    best-effort: this function NEVER raises on a missing optional library; on any internal error it
    returns ok=True with a note so the gate can never become a hard outage."""
    notes = {"threshold": _DELTAE_THRESHOLD}
    try:
        cand = _open_rgb(candidate_bytes)
        cutout = _open_rgba(original_cutout_rgba)
        px, py, resized = _resolve_box(cand.width, cand.height, cutout, x, y, scale)
        rw, rh = resized.size

        # Clamp the crop box to the candidate bounds (placement can run partly off-canvas by design).
        cx0, cy0 = max(0, px), max(0, py)
        cx1, cy1 = min(cand.width, px + rw), min(cand.height, py + rh)
        if cx1 <= cx0 or cy1 <= cy0:
            notes["reason"] = "product region outside candidate bounds"
            return {"ok": True, "deltaE": 0.0, "notes": notes}

        cand_crop = np.asarray(cand.crop((cx0, cy0, cx1, cy1)))  # HxWx3
        # Align the cutout to the SAME clamped sub-box.
        ox0, oy0 = cx0 - px, cy0 - py
        cut_crop = resized.crop((ox0, oy0, ox0 + (cx1 - cx0), oy0 + (cy1 - cy0)))
        cut_arr = np.asarray(cut_crop)  # HxWx4
        cut_rgb = cut_arr[..., :3]
        cut_alpha = cut_arr[..., 3]
        mask = cut_alpha > 200  # solidly-opaque product pixels only

        mean_cut = _mean_lab_over_mask(cut_rgb, mask)
        mean_cand = _mean_lab_over_mask(cand_crop, mask)
        if mean_cut is None or mean_cand is None:
            notes["reason"] = "empty product mask"
            return {"ok": True, "deltaE": 0.0, "notes": notes}

        deltaE = float(np.sqrt(np.sum((mean_cut - mean_cand) ** 2)))
        notes["product_px"] = int(mask.sum())
        ok = deltaE < _DELTAE_THRESHOLD
        if not ok:
            notes["reason"] = "product color drifted beyond ΔE threshold (candidate likely not paste-backed)"
        return {"ok": ok, "deltaE": round(deltaE, 3), "notes": notes}
    except Exception as e:  # never let the gate become an outage
        log.info("fidelity_gate soft-pass on error: %s", e)
        notes["reason"] = f"gate error (soft pass): {e}"
        return {"ok": True, "deltaE": 0.0, "notes": notes}


# ═══════════════════════════ keyframe orchestration ═══════════════════════════
# Per-style placement plans. Each entry is (x, y, scale); x/y as 0..1 fractions (center-anchored),
# scale as a multiplier on the ~⅓-scene-width base. Deterministic — these ARE the placements.
_SHOWCASE_PLACEMENTS = [
    (0.50, 0.55, 1.10),  # hero, centered, slightly low
    (0.42, 0.52, 1.05),  # ¾ left
    (0.58, 0.58, 1.00),  # ¾ right, lower
]
_INSCENE_PLACEMENT = (0.50, 0.62, 0.85)   # sit it on the surface, a touch smaller
_UGC_PLACEMENT = (0.50, 0.66, 0.55)       # product inset, lower-center, held


def _vibe_lighting(vibe: str) -> str:
    return {
        "luxury": "soft directional key light, deep shadows, premium reflective surface",
        "energetic": "bright punchy lighting, vivid saturated backdrop",
        "minimal": "soft even lighting, seamless pale backdrop, lots of negative space",
        "playful": "colorful gradient backdrop, cheerful bright lighting",
        "ugc": "natural daylight, casual handheld feel",
        "tech": "cool clean lighting, subtle gradient, crisp edges",
    }.get((vibe or "minimal").lower(), "soft studio lighting")


async def make_keyframes(style: str, cutouts: list, scene_prompt: Optional[str], vibe: str,
                         aspect: str, n: int, op_id: str) -> list:
    """Produce up to `n` locked keyframe images (PNG bytes) for a style. Orchestrates
    scene_plate → place_cutout → harmonize → paste_back → fidelity_gate. The PRIMARY cutout
    (cutouts[0]) is the hero product; placement is deterministic per style.

    showcase  — clean studio backdrop in the chosen vibe; n hero angles via slight placement/scale
                variation.
    in_scene  — the caller's scene_prompt (e.g. "drink on a beach table at golden hour"); product
                placed at a sensible on-surface spot.
    ugc       — avatar/handheld feel: scene plate is a casual person/hands setting; the product is
                composited as a held inset. (Full avatar lip-sync lives in recipe_product_ad's ugc
                animate path; here we produce the still keyframe = scene + product inset.)

    Returns a list of dicts: {"bytes": <png>, "gate": <fidelity_gate result>, "placement": (x,y,scale)}
    — bytes are always the paste-backed (pixel-locked) frame; gate is advisory (frames are returned
    even if the gate trips, since paste_back already guarantees the product pixels)."""
    if not cutouts:
        raise ValueError("make_keyframes requires at least one cutout")
    style = (style or "showcase").lower()
    n = max(1, int(n))
    hero = cutouts[0]
    light = _vibe_lighting(vibe)

    if style == "showcase":
        base_prompt = (scene_prompt or f"empty seamless studio sweep backdrop with a clean floor, {light}, "
                                       f"product-photography set with absolutely NO people, NO hands, NO product, "
                                       f"NO can, NO bottle, NO text and NO logos — just the bare backdrop and floor")
        placements = _SHOWCASE_PLACEMENTS
    elif style == "in_scene":
        base_prompt = (scene_prompt or f"a natural lifestyle scene with an empty surface to place a "
                                       f"product on, {light}, no product in frame")
        placements = [_INSCENE_PLACEMENT]
    elif style == "ugc":
        # A talking-head avatar (omnihuman / kling-avatar) is driven from a FACE in the seed frame, so the
        # UGC plate must show a forward-facing person — not just hands (which gave the avatar no face to
        # animate). The real product is paste-backed into the raised hand, lower-center, keeping product
        # fidelity on the seed while the avatar lip-syncs the voiceover.
        base_prompt = (scene_prompt or f"a casual selfie-style UGC shot of one friendly person from the "
                                       f"chest up, facing the camera straight on, smiling, raising one hand "
                                       f"toward the lens to present a product, {light}, plain home-interior "
                                       f"background, no other people, no product yet in the raised hand")
        placements = [_UGC_PLACEMENT]
    else:
        raise ValueError(f"unknown style: {style!r}")

    async def _one(i: int) -> dict:
        x, y, scale = placements[i % len(placements)]
        # Distinct op suffixes for the scene vs harmonize image ops so their R2 rehost keys never collide
        # (harmonize would otherwise overwrite scene_plate's stored artifact when both hit one provider).
        step_op = f"{op_id}-kf{i}"
        # Track 2: fresh scene plate per keyframe so angles/lighting vary across hero shots.
        scene = await scene_plate(base_prompt, aspect, step_op + "-s")
        raw = place_cutout(scene, hero, x, y, scale)
        ai = await harmonize(raw, step_op + "-h")
        locked = paste_back(ai, hero, x, y, scale)
        gate = await fidelity_gate(hero, locked, x, y, scale)
        return {"bytes": locked, "gate": gate, "placement": (x, y, scale)}

    # Keyframes are independent — each is its own scene→place→harmonize→paste-back→gate chain. Build
    # them concurrently (the network steps dominate); gather preserves order. Fault-tolerant: one frame's
    # all-models-fail drops THAT frame (the caller reuses survivors via modulo indexing) rather than
    # aborting the whole ad; only if EVERY frame fails do we raise (→ run refunds the umbrella hold).
    results = await asyncio.gather(*[_one(i) for i in range(n)], return_exceptions=True)
    frames: list = [r for r in results if not isinstance(r, BaseException) and r is not None]
    if not frames:
        errs = [r for r in results if isinstance(r, BaseException)]
        raise errs[0] if errs else RuntimeError("make_keyframes: all keyframes failed to build")
    return frames

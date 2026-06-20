# Whiteboard render backends (Guide-2 §K/§J/§L)

The contract is `whiteboard_visual_plan` → `resolvePlan` → a render BACKEND. Backends are
interchangeable; the plan (not any renderer) is the source of truth.

| Backend | File | Status | Notes |
|---|---|---|---|
| **remotion** | `../render.mjs` (`renderWhiteboardPlan`) | ✅ **PRODUCTION** — the wired default | Headless Chromium; all genres, draw-on, raster-reveal, camera, rough pass |
| **svg_ffmpeg** | `svgFfmpeg.mjs` | ✅ **WIRED + VERIFIED (opt-in)** — `WB_RENDER_BACKEND=svg_ffmpeg` | No Chromium. Rasterizes via `@resvg/resvg-js` (pure-Rust, in deps) → ffmpeg. `buildSceneSvg` covers diagram cards+badges+arrows, icon draw-on, **rough pass, raster-reveal (image+clip-wipe), camera, overlays**. `renderWhiteboardPlanSvg` mirrors Remotion (per-scene MP4+audio → concat). Verified locally: diagram/cycle/rough/raster all render; 2-scene→8s MP4 w/ audio. stitchProcessor falls back to Remotion on any error. |
| **manim** | `manim.mjs` + `../../../python/whiteboard/manim_generators/` | 🧪 SCAFFOLD — unverified | Specialized math/science only. Needs `manim` + LaTeX/Cairo (heavy, not in the image). Controlled templates only (LLM fills data, never writes Manim code). |

`dispatch.mjs` picks the backend from `engine_hint` / `animation.backend_preference`. It currently
routes **everything to Remotion** (the proven path — roadmap §B: "Default tetap Remotion enhanced
sampai SVG/FFmpeg backend terbukti lebih murah/stabil"). `pickBackend(hint, {allowExperimental})`
only returns svg_ffmpeg/manim when a caller explicitly opts in; nothing in the live worker calls
the dispatcher yet — it is the seam the experimental backends plug into once verified.

## svg_ffmpeg — DONE (2026-06-20)
1. ✅ `@resvg/resvg-js` in deps. 2. ✅ verified locally → MP4. 3. ✅ raster-reveal + camera + rough ported.
4. ✅ wired in `workers.stitchProcessor` via `WB_RENDER_BACKEND` (default remotion; svg_ffmpeg opt-in;
   auto-fallback to Remotion on error). Remaining polish: tune raster-reveal to follow the mask
   frontier (currently a left→right clip wipe) and add the hand sprite if desired.

## To productionize manim
1. Add `manim` + LaTeX to the worker Docker image (heavy — consider a separate service).
2. Add a `math_explainer` template + the route emitting `geometry.source:"manim"` for math scenes.
3. Wire into the dispatcher under `allowExperimental`.

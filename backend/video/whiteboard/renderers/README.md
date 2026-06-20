# Whiteboard render backends (Guide-2 §K/§J/§L)

The contract is `whiteboard_visual_plan` → `resolvePlan` → a render BACKEND. Backends are
interchangeable; the plan (not any renderer) is the source of truth.

| Backend | File | Status | Notes |
|---|---|---|---|
| **remotion** | `../render.mjs` (`renderWhiteboardPlan`) | ✅ **PRODUCTION** — the wired default | Headless Chromium; all genres, draw-on, raster-reveal, camera, rough pass |
| **svg_ffmpeg** | `svgFfmpeg.mjs` | 🧪 SCAFFOLD — frame-builder verified, render unverified | No Chromium. `buildSceneSvg` is pure + tested (diagram path: cards+labels+arrows+icon strokes). `renderSceneSvgFfmpeg` needs a system rasterizer (`@resvg/resvg-js`, `sharp`, or `librsvg`) — none in the image yet. TODO: raster-reveal, camera, rough. |
| **manim** | `manim.mjs` + `../../../python/whiteboard/manim_generators/` | 🧪 SCAFFOLD — unverified | Specialized math/science only. Needs `manim` + LaTeX/Cairo (heavy, not in the image). Controlled templates only (LLM fills data, never writes Manim code). |

`dispatch.mjs` picks the backend from `engine_hint` / `animation.backend_preference`. It currently
routes **everything to Remotion** (the proven path — roadmap §B: "Default tetap Remotion enhanced
sampai SVG/FFmpeg backend terbukti lebih murah/stabil"). `pickBackend(hint, {allowExperimental})`
only returns svg_ffmpeg/manim when a caller explicitly opts in; nothing in the live worker calls
the dispatcher yet — it is the seam the experimental backends plug into once verified.

## To productionize svg_ffmpeg
1. Add a rasterizer dep (`@resvg/resvg-js` recommended — pure-Rust, no system libs) to `backend/package.json`.
2. Verify `renderSceneSvgFfmpeg` on a resolved diagram plan locally → MP4.
3. Port raster-reveal (mask wipe) + camera + the rough pass into `buildSceneSvg`.
4. Wire `dispatch.renderScene` into `workers.stitchProcessor` with `{remotion, svg_ffmpeg}` impls,
   gated by `engine_hint`/an env flag; keep Remotion default.

## To productionize manim
1. Add `manim` + LaTeX to the worker Docker image (heavy — consider a separate service).
2. Add a `math_explainer` template + the route emitting `geometry.source:"manim"` for math scenes.
3. Wire into the dispatcher under `allowExperimental`.

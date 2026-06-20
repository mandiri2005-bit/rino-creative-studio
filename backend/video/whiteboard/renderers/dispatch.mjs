// renderers/dispatch.mjs — the render BACKEND dispatcher (Guide-2 §L).
//
// The contract is whiteboard_visual_plan, NOT any single renderer. This picks which backend
// renders a resolved scene, from `engine_hint` / `animation.backend_preference` (Guide-2 §E).
//
// CURRENT STATE: Remotion (the proven plan-engine renderer in render.mjs) is the ONLY wired
// backend and the default for everything — exactly as the roadmap prescribes ("Default tetap
// Remotion enhanced sampai SVG/FFmpeg backend terbukti lebih murah/stabil"). svg_ffmpeg and
// manim are SCAFFOLDED (see svgFfmpeg.mjs / ../../../python/whiteboard/manim_generators) but
// NOT production-verified, so this dispatcher falls back to Remotion for them with a warning.
// Nothing in the live worker path calls this yet — it is the seam future backends plug into.

export const BACKENDS = ["remotion", "svg_ffmpeg", "manim"];

// Decide the backend for ONE scene. `hint` = plan.engine_hint || animation.backend_preference.
// "auto" (and anything unknown/unwired) → remotion.
export function pickBackend(hint, { allowExperimental = false } = {}) {
  const h = String(hint || "auto").toLowerCase();
  if (h === "manim" && allowExperimental) return "manim";
  if (h === "svg_ffmpeg" && allowExperimental) return "svg_ffmpeg";
  if (h === "remotion") return "remotion";
  return "remotion"; // auto / unknown / experimental-not-allowed
}

// Render a resolved scene with the chosen backend. Only remotion is wired; the others throw so a
// caller that opts in (allowExperimental) fails loudly rather than silently producing nothing.
// `backends` lets the worker inject the wired implementations (keeps this module import-light).
export async function renderScene(resolvedScene, ctx, backends = {}) {
  const backend = pickBackend(
    resolvedScene.engine_hint || resolvedScene.animation?.backend_preference,
    { allowExperimental: !!ctx?.allowExperimental }
  );
  const impl = backends[backend];
  if (!impl) {
    if (backend !== "remotion" && backends.remotion) {
      // experimental backend requested but unwired here → fall back to the proven one
      return backends.remotion(resolvedScene, ctx);
    }
    throw new Error(`renderScene: no implementation for backend "${backend}"`);
  }
  return impl(resolvedScene, ctx);
}

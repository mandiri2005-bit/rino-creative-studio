// resolvePlan — the keystone. Turns a renderer-independent whiteboard_visual_plan into a
// fully RESOLVED, render-ready plan (Node-side, so it's unit-testable WITHOUT Remotion):
//   validate → resolve each element's asset_query to SVG strokes → semantic slot to box →
//   beats/camera to frame windows. The Remotion composition just draws what's here.

import { readFileSync } from "node:fs";
import { parseSvg } from "../svg.mjs";
import { loadManifest, resolveAssetPath } from "./resolver.mjs";
import { layoutWhiteboardPlan } from "./slots.mjs";
import { validateWhiteboardPlan } from "./validate.mjs";
import { secondsToFrames, drawBeatFor } from "./beats.mjs";
import { DEFAULT_FPS, DEFAULT_CANVAS } from "./schema.mjs";
import { resolveStylePack } from "./stylePacks.mjs";
import { resolveLucide } from "./lucide.mjs";

export function resolvePlan(planOrPath, { assetsDir, fps = DEFAULT_FPS, strict = true } = {}) {
  const plan = typeof planOrPath === "string" ? JSON.parse(readFileSync(planOrPath, "utf8")) : planOrPath;

  const validation = validateWhiteboardPlan(plan);
  if (!validation.ok && strict) {
    throw new Error("Invalid whiteboard plan:\n - " + validation.errors.join("\n - "));
  }

  const manifest = loadManifest(assetsDir);
  const pack = resolveStylePack(plan.style_pack);          // §O — palette/stroke/font
  const canvas = { ...DEFAULT_CANVAS, ...(plan.canvas || {}) };
  const duration = Number(plan.duration) || 0;
  const durationInFrames = Math.max(1, secondsToFrames(duration, fps));
  const laid = layoutWhiteboardPlan(plan); // attaches `box`; throws on unknown slot
  const boxOf = (id) => laid.elements.find((e) => e.id === id)?.box || null;

  const elements = laid.elements.map((el) => {
    const query = el.asset_query || el.id;
    let viewBox = "0 0 100 100";
    let strokes = [];
    let assetId = null;
    let assetSource = "none";
    let fallback = true;

    // Asset fallback ladder (guide §J): pre-baked strokes (Recraft on-miss, visual phase) →
    // curated manifest (strong tag match) → Lucide (1737) → generic placeholder.
    if (Array.isArray(el.strokes) && el.strokes.length) {
      // already resolved upstream (e.g. Recraft generate-on-miss baked strokes into the plan)
      viewBox = el.viewBox || "0 0 100 100";
      strokes = el.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
      assetId = el.assetId || "prebaked"; assetSource = el.assetSource || "prebaked"; fallback = false;
    } else {
      const r = resolveAssetPath(query, manifest);
      if (!r.fallback && r.path) {
        const parsed = parseSvg(readFileSync(r.path, "utf8"), { ink: pack.palette.ink });
        viewBox = parsed.viewBox;
        strokes = parsed.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
        assetId = r.asset?.id || null; assetSource = "manifest"; fallback = false;
      } else {
        const lu = resolveLucide(query, { ink: pack.palette.ink, width: pack.stroke.width });
        if (lu) {
          viewBox = lu.viewBox; strokes = lu.strokes; assetId = "lucide:" + lu.name; assetSource = "lucide"; fallback = false;
        } else if (r.path) {
          const parsed = parseSvg(readFileSync(r.path, "utf8"), { ink: pack.palette.ink }); // generic_concept
          viewBox = parsed.viewBox;
          strokes = parsed.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
          assetId = r.asset?.id || "generic"; assetSource = "generic"; fallback = true;
        }
      }
    }

    const beat = drawBeatFor(el.id, plan.beats, Math.min(1.5, duration));
    return {
      id: el.id,
      type: el.type || "icon",
      slot: el.slot,
      box: el.box,
      label: el.label || null,
      assetId,
      assetSource,
      fallback,
      viewBox,
      strokes,
      draw: {
        startFrame: secondsToFrames(beat.start, fps),
        durFrames: Math.max(1, secondsToFrames(beat.end - beat.start, fps)),
      },
    };
  });

  const overlays = (plan.beats || [])
    .filter((b) => b.action === "highlight_circle" || b.action === "underline")
    .map((b) => {
      const box = boxOf(b.target);
      if (!box) return null;
      return {
        kind: b.action,
        target: b.target,
        box,
        startFrame: secondsToFrames(b.start, fps),
        durFrames: Math.max(1, secondsToFrames(b.end - b.start, fps)),
      };
    })
    .filter(Boolean);

  const camera = (plan.camera || []).map((c) => {
    const box = c.target === "full_canvas" ? { x: canvas.width / 2, y: canvas.height / 2 } : boxOf(c.target);
    return {
      type: c.type,
      scale: Number(c.scale) || 1,
      startFrame: secondsToFrames(c.start, fps),
      endFrame: secondsToFrames(c.end, fps),
      cx: box ? box.x : canvas.width / 2,
      cy: box ? box.y : canvas.height / 2,
    };
  });

  // Render mode (genre → how the scene is drawn): "icons" (default), "diagram" (boxes + arrows
  // flowchart), "raster" (Recraft photos revealed through a mask). Genre maps in render.mjs.
  const mode = plan.mode || "icons";

  // For diagram mode: arrows connect elements in DRAW order (visual flow), appearing after the
  // source element is drawn. Computed here so the composition stays dumb.
  const ordered = [...elements].sort((a, b) => a.draw.startFrame - b.draw.startFrame);
  const connectors = mode !== "diagram" ? [] : ordered.slice(0, -1).map((a, i) => {
    const b = ordered[i + 1];
    return { from: a.box, to: b.box, startFrame: Math.max(a.draw.startFrame + a.draw.durFrames, b.draw.startFrame - 8), durFrames: 14 };
  });

  return {
    scene_id: plan.scene_id,
    template: plan.template,
    style_pack: pack.name,
    stylePack: pack,                                       // resolved palette/stroke/font for the renderer
    mode,
    connectors,
    fps,
    duration,
    durationInFrames,
    canvas,
    background: (plan.canvas && plan.canvas.background) || "whiteboard_clean",
    elements,
    overlays,
    camera,
    validation,
  };
}

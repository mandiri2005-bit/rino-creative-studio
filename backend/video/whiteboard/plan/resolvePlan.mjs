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
    const r = resolveAssetPath(el.asset_query || el.id, manifest);
    let viewBox = "0 0 100 100";
    let strokes = [];
    if (r.path) {
      const parsed = parseSvg(readFileSync(r.path, "utf8"), { ink: pack.palette.ink }); // recolour to the pack's ink
      viewBox = parsed.viewBox;
      strokes = parsed.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
    }
    const beat = drawBeatFor(el.id, plan.beats, Math.min(1.5, duration));
    return {
      id: el.id,
      type: el.type || "icon",
      slot: el.slot,
      box: el.box,
      label: el.label || null,
      assetId: r.asset?.id || null,
      fallback: r.fallback,
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

  return {
    scene_id: plan.scene_id,
    template: plan.template,
    style_pack: pack.name,
    stylePack: pack,                                       // resolved palette/stroke/font for the renderer
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

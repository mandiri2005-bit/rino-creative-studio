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
import { resolveIcon } from "./iconlibs.mjs";

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
  const laid = layoutWhiteboardPlan(plan); // attaches `box`; auto-grids unknown/missing slots (no throw)
  const mode = plan.mode || "icons";
  const direction = (plan.direction === "down" || plan.direction === "vertical") ? "down" : "right";
  // color genre: free libs are monochrome, so COLORIZE icons with the pack palette (cycle per
  // element) + a soft colour chip behind each → "berwarna" look without paying for Recraft.
  const colorize = mode === "color";
  const PALETTE = [pack.palette.accent, pack.palette.highlight, pack.palette.success, pack.palette.warning].filter(Boolean);
  // Diagram LAYOUT — flow (down|right line) is default; cycle/funnel/branch are richer shapes
  // the LLM picks to fit the content (loop / narrowing / one-to-many). All keep nodes BIG and
  // draw on in sequence; arrows are auto-derived per layout below.
  const layout = ["cycle", "funnel", "branch"].includes(plan.layout) ? plan.layout : "flow";

  // Connector/arrow elements are flow FILLER the LLM inserts between concepts. In DIAGRAM the arrows
  // are auto-drawn (below), so the LLM's ↑ icons are noise; in icons/color/lineart a connector
  // resolves to a stray "arrow-up" chip (asset_query "arrow"/"arrow right" all score to tabler:
  // arrow-up). Drop them in EVERY mode → a clean concept row / flowchart, never random ↑ icons.
  // (Was the "panah ke arah atas" + "kadang ada kadang ngga" bug: only diagram filtered, not icons.)
  const isConnectorEl = (e) => /^connector/i.test(e.slot || "") || e.type === "arrow" || e.type === "connector";
  let workEls = laid.elements.filter((e) => !isConnectorEl(e));
  if (mode === "diagram") {
    const nodes = workEls; // already connector-free
    const n = Math.max(1, nodes.length);
    const cx = Math.round(canvas.width * 0.5), cy = Math.round(canvas.height * 0.5);
    if (layout === "cycle") {
      // nodes evenly on a circle, starting at top, going clockwise; arrows close the loop
      const R = Math.round(Math.min(canvas.width, canvas.height) * 0.34);
      const sz = n <= 4 ? 240 : n <= 6 ? 210 : 180;
      workEls = nodes.map((e, i) => {
        const ang = -Math.PI / 2 + i * ((2 * Math.PI) / n);
        return { ...e, box: { x: Math.round(cx + R * Math.cos(ang)), y: Math.round(cy + R * Math.sin(ang)), w: sz, h: sz } };
      });
    } else if (layout === "funnel") {
      // stacked bars narrowing downward; sequential down arrows
      const maxW = Math.round(canvas.width * 0.62), minW = Math.round(canvas.width * 0.24);
      const gap = 26;
      const barH = Math.min(200, Math.floor((canvas.height - 150 - gap * (n - 1)) / n));
      const totalH = n * barH + (n - 1) * gap;
      const y0 = Math.round((canvas.height - totalH) / 2) + barH / 2;
      workEls = nodes.map((e, i) => {
        const w = n === 1 ? maxW : Math.round(maxW - (maxW - minW) * (i / (n - 1)));
        return { ...e, box: { x: cx, y: Math.round(y0 + i * (barH + gap)), w, h: barH } };
      });
    } else if (layout === "branch") {
      // root on top, the rest fanned as children below; arrows root → each child
      const rootW = Math.min(420, Math.round(canvas.width * 0.24)), rootH = 200, childH = 210;
      const rootY = Math.round(canvas.height * 0.21), childY = Math.round(canvas.height * 0.68);
      const kids = Math.max(1, n - 1);
      const cgap = 44;
      const childW = Math.min(360, Math.floor((canvas.width - 120 - cgap * (kids - 1)) / kids));
      const totalW = kids * childW + (kids - 1) * cgap;
      const x0 = Math.round((canvas.width - totalW) / 2) + childW / 2;
      workEls = nodes.map((e, i) => {
        if (i === 0) return { ...e, box: { x: cx, y: rootY, w: rootW, h: rootH } };
        const k = i - 1;
        return { ...e, box: { x: Math.round(x0 + k * (childW + cgap)), y: childY, w: childW, h: childH } };
      });
    } else if (direction === "down") {
      const gap = 90; // big enough that the vertical arrow has a visible shaft between cards
      const boxW = Math.min(660, Math.round(canvas.width * 0.42));
      const boxH = Math.min(200, Math.floor((canvas.height - 140 - gap * (n - 1)) / n));
      const x = cx;
      const totalH = n * boxH + (n - 1) * gap;
      const y0 = Math.round((canvas.height - totalH) / 2) + boxH / 2;
      workEls = nodes.map((e, i) => ({ ...e, box: { x, y: Math.round(y0 + i * (boxH + gap)), w: boxW, h: boxH } }));
    } else {
      const margin = 70, gap = 110; // wider gap → the arrow has a visible shaft (a real "→", not just ">")
      const boxW = Math.min(380, Math.floor((canvas.width - margin * 2 - gap * (n - 1)) / n));
      const boxH = Math.min(360, Math.round(boxW * 1.0));
      const y = cy;
      const totalW = n * boxW + (n - 1) * gap;
      const x0 = Math.round((canvas.width - totalW) / 2) + boxW / 2;
      workEls = nodes.map((e, i) => ({ ...e, box: { x: Math.round(x0 + i * (boxW + gap)), y, w: boxW, h: boxH } }));
    }
  }
  const boxOf = (id) => workEls.find((e) => e.id === id)?.box || null;

  const elements = workEls.map((el, elIdx) => {
    const query = el.asset_query || el.id;
    let viewBox = "0 0 100 100";
    let strokes = [];
    let libShapes = null; // filled icons from a fill lib (Phosphor) render as shapes, not strokes
    let assetId = null;
    let assetSource = "none";
    let license = null;   // provenance/license trail (commercial-safety at scale, guide §P/§S)
    let fallback = true;

    // Asset fallback ladder (guide §J): raster-reveal (genre detail, Recraft photo + mask) →
    // pre-baked strokes (Recraft on-miss) → curated manifest → Lucide (1737) → generic.
    if (el.raster) {
      assetSource = el.assetSource || "recraft-raster"; license = el.license || "generated:provider-terms"; fallback = false; // raster + mask carried in the return
    } else if (Array.isArray(el.strokes) && el.strokes.length) {
      // already resolved upstream (e.g. Recraft generate-on-miss baked strokes into the plan)
      viewBox = el.viewBox || "0 0 100 100";
      strokes = el.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
      assetId = el.assetId || "prebaked"; assetSource = el.assetSource || "prebaked"; license = el.license || "generated:provider-terms"; fallback = false;
    } else {
      const r = resolveAssetPath(query, manifest);
      if (!r.fallback && r.path) {
        const parsed = parseSvg(readFileSync(r.path, "utf8"), { ink: pack.palette.ink });
        viewBox = parsed.viewBox;
        strokes = parsed.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
        assetId = r.asset?.id || null; assetSource = "manifest"; license = r.asset?.license || "curated"; fallback = false;
      } else {
        const lib = resolveIcon(query, { ink: pack.palette.ink, width: pack.stroke.width });
        if (lib) {
          viewBox = lib.viewBox; assetId = `${lib.lib}:${lib.name}`; assetSource = lib.lib; license = lib.license || "unknown"; fallback = false;
          if (lib.strokes) strokes = lib.strokes;       // lucide / tabler → self-draw strokes
          if (lib.shapes) libShapes = lib.shapes;       // phosphor → filled silhouette
        } else if (r.path) {
          const parsed = parseSvg(readFileSync(r.path, "utf8"), { ink: pack.palette.ink }); // generic_concept
          viewBox = parsed.viewBox;
          strokes = parsed.strokes.map((s) => ({ d: s.d, stroke: s.stroke || pack.palette.ink, width: s.width || pack.stroke.width }));
          assetId = r.asset?.id || "generic"; assetSource = "generic"; license = "internal"; fallback = true;
        }
      }
    }

    // Normalise stroke width to the icon's viewBox so Lucide (vb 24), Recraft (vb 1024) and
    // curated SVGs (vb 100) all render at the SAME visual thickness — fixes "Lucide too thick /
    // Recraft too thin". pack.stroke.width is calibrated for a 100-unit viewBox.
    const sw = Math.max(0.6, (parseFloat(viewBox.split(/\s+/)[2]) || 100) * (pack.stroke.width / 100));
    strokes = strokes.map((s) => ({ ...s, width: sw }));

    // color genre: a per-element palette colour for the chip AND the icon's FILL — so a mono icon gets
    // colour back (Rino: "warna aslinya hilang"). Genuinely multi-colour iconify icons keep their own
    // colours. The OUTLINE stays dark ink + thin (renderer) → reads as black linework + colour fill,
    // not a recoloured blob.
    let chip = null;
    if (colorize && PALETTE.length && assetSource !== "recraft" && !el.raster) {
      const c = PALETTE[elIdx % PALETTE.length];
      chip = c;
      if (libShapes && libShapes.length) libShapes = libShapes.map((s) => ({ ...s, fill: (s.fill && s.fill !== pack.palette.ink) ? s.fill : c }));
    }

    const beat = drawBeatFor(el.id, plan.beats, Math.min(1.5, duration));
    return {
      id: el.id,
      type: el.type || "icon",
      slot: el.slot,
      box: el.box,
      // never leave an element textless: if the VD omitted the label, derive one from the asset_query
      // (Title Case) so a scene is never "no text" (Rino).
      label: el.label || (el.asset_query ? String(el.asset_query).replace(/\b\w/g, (c) => c.toUpperCase()) : null),
      assetId,
      assetSource,
      license,
      fallback,
      viewBox,
      strokes,
      ...(chip ? { chip } : {}),   // color genre: soft colour chip behind the icon
      // colored fills: from upstream baking (Recraft/color) OR a fill lib (Phosphor silhouette)
      ...((Array.isArray(el.shapes) && el.shapes.length) ? { shapes: el.shapes }
        : (libShapes && libShapes.length) ? { shapes: libShapes } : {}),
      ...(el.raster ? {
        raster: el.raster,
        maskViewBox: el.maskViewBox || el.viewBox || "0 0 100 100",
        maskStrokes: el.maskStrokes || [],
        maskShapes: el.maskShapes || [],
      } : {}),
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

  // Diagram arrows: between consecutive NODES in layout order (the flow), each drawing on after
  // its source node. The Connector component derives the arrow angle from from/to, so down|right
  // both work. (mode + direction resolved at the top.)
  // Arrows derived per LAYOUT. The Connector component computes the edge intersection from the
  // box centres, so centre anchors work for any direction. startFrame chains after the source.
  const ctr = (box) => ({ x: box.x, y: box.y, w: box.w, h: box.h });
  const sf = (a, b) => Math.max(a.draw.startFrame + a.draw.durFrames, b.draw.startFrame - 8);
  let connectors = [];
  if (mode === "diagram" && elements.length > 1) {
    if (layout === "cycle") {
      // ring: i → (i+1) mod n, closing the loop back to the first node
      connectors = elements.map((a, i) => {
        const b = elements[(i + 1) % elements.length];
        return { from: ctr(a.box), to: ctr(b.box), startFrame: sf(a, b), durFrames: 14 };
      });
    } else if (layout === "branch") {
      // star: root (element 0) → each child
      const root = elements[0];
      connectors = elements.slice(1).map((b) => ({ from: ctr(root.box), to: ctr(b.box), startFrame: sf(root, b), durFrames: 14 }));
    } else {
      // flow / funnel: sequential. RIGHT flow drops the arrow to the lower third (below the
      // label, above the line); vertical flows go centre-to-centre (edge to edge).
      const vertical = layout === "funnel" || direction === "down";
      const anchor = vertical ? ctr : (box) => ({ x: box.x, y: Math.round(box.y + box.h * 0.27), w: box.w, h: box.h });
      connectors = elements.slice(0, -1).map((a, i) => {
        const b = elements[i + 1];
        return { from: anchor(a.box), to: anchor(b.box), startFrame: sf(a, b), durFrames: 14 };
      });
    }
  }

  return {
    scene_id: plan.scene_id,
    template: plan.template,
    style_pack: pack.name,
    stylePack: pack,                                       // resolved palette/stroke/font for the renderer
    mode,
    layout,
    style_pass: plan.style_pass || { mode: "clean" },   // §H — drives the rough hand-drawn pass
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

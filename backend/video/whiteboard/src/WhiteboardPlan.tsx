import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { SelfDrawSvg } from "./components/SelfDrawSvg";
import { RasterRevealIllustration } from "./components/RasterRevealIllustration";

// The "dumb" renderer for a RESOLVED whiteboard plan (produced by scripts/lib/plan/
// resolvePlan.mjs on the Node side). It only DRAWS what's already planned: per-element
// stroke draw-on (hand follows the pen), write-on labels, highlight overlays, and a
// camera transform. No resolving/layout here — that all happened upstream.

const INK = "#1F2937";
const BOARD = "#FBFBF7";
const ACCENT = "#F59E0B";

export interface PlanBox { x: number; y: number; w: number; h: number }
export interface PlanStroke { d: string; stroke?: string; width?: number }
export interface PlanShape { d: string; fill?: string; stroke?: string; width?: number }
export interface PlanElement {
  id: string;
  box: PlanBox;
  label: string | null;
  viewBox: string;
  strokes: PlanStroke[];
  shapes?: PlanShape[];       // colored fills (Recraft/color) drawn under the ink strokes
  // raster-reveal (genre "detail"): the original Recraft photo revealed through a vector mask
  raster?: string;            // data URI / URL
  maskViewBox?: string;
  maskStrokes?: PlanStroke[];
  maskShapes?: PlanShape[];
  _roughBorder?: string;      // rough-style-pass: baked wobbly card-border path `d` (local coords)
  chip?: string;              // color genre: soft colour chip behind the icon
  draw: { startFrame: number; durFrames: number };
}
export interface PlanOverlay { kind: string; box: PlanBox; startFrame: number; durFrames: number }
export interface PlanCamera { type: string; scale: number; startFrame: number; endFrame: number; cx: number; cy: number }
export interface StylePack {
  board?: string;
  palette?: { ink?: string; accent?: string; highlight?: string };
  stroke?: { width?: number };
  font?: { label?: string; weight?: number; labelSize?: number };
}
export interface PlanConnector { from: PlanBox; to: PlanBox; startFrame: number; durFrames: number; _roughShaft?: string }
export interface ResolvedPlan {
  fps: number;
  durationInFrames: number;
  canvas: { width: number; height: number };
  elements: PlanElement[];
  overlays: PlanOverlay[];
  camera: PlanCamera[];
  stylePack?: StylePack;
  mode?: string;                 // "icons" (default) | "diagram" (boxes+arrows) | "raster"
  connectors?: PlanConnector[];  // diagram-mode arrows between elements (draw order)
}
const DEFAULT_PACK: Required<StylePack> = {
  board: BOARD,
  palette: { ink: INK, accent: ACCENT, highlight: ACCENT },
  stroke: { width: 4 },
  font: { label: "Inter, system-ui, sans-serif", weight: 800, labelSize: 34 },
};

function easeInOut(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

// Camera = translate the target's center to the canvas centre, scaled. Between windows we
// ease from the previous window's end-state to the active window's target.
function cameraTransform(camera: PlanCamera[], frame: number, canvas: { width: number; height: number }): string {
  const cx0 = canvas.width / 2;
  const cy0 = canvas.height / 2;
  const stateOf = (c: PlanCamera) => ({ dx: cx0 - c.cx, dy: cy0 - c.cy, s: c.scale });
  const active = camera.find((c) => frame >= c.startFrame && frame <= c.endFrame);
  if (!active) {
    const past = [...camera].filter((c) => frame > c.endFrame).pop();
    if (past) { const p = stateOf(past); return `translate(${p.dx}px, ${p.dy}px) scale(${p.s})`; }
    return "translate(0px, 0px) scale(1)";
  }
  const prev = [...camera].filter((c) => c.endFrame <= active.startFrame).pop();
  const from = prev ? stateOf(prev) : { dx: 0, dy: 0, s: 1 };
  const to = stateOf(active);
  const t = easeInOut(
    interpolate(frame, [active.startFrame, active.endFrame], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" })
  );
  const dx = from.dx + (to.dx - from.dx) * t;
  const dy = from.dy + (to.dy - from.dy) * t;
  const s = from.s + (to.s - from.s) * t;
  return `translate(${dx}px, ${dy}px) scale(${s})`;
}

const WriteOnText: React.FC<{ text: string; startFrame: number; pack: Required<StylePack>; top: number }> = ({ text, startFrame, pack, top }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const charsPerSec = 16;
  const shown = Math.max(0, Math.floor(((frame - startFrame) / fps) * charsPerSec));
  return (
    <div
      style={{
        position: "absolute", top, left: -30, width: "calc(100% + 60px)", textAlign: "center",
        fontFamily: pack.font.label, fontWeight: pack.font.weight, fontSize: pack.font.labelSize,
        lineHeight: 1.1, color: pack.palette.ink, opacity: frame >= startFrame ? 1 : 0, letterSpacing: "-0.01em",
      }}
    >
      {text.slice(0, shown)}
    </div>
  );
};

// Colored fills (Recraft/color/Phosphor/Iconify). DRAW each shape: trace its outline (pathLength-
// normalised, like SelfDrawSvg) then fill it in over the back half of its window, SEQUENTIALLY — so
// FILL icons self-draw like the line icons instead of just fading in (was the "iconify pops, doesn't
// draw" bug). Rendered under the ink strokes.
const FilledShapes: React.FC<{ shapes: PlanShape[]; viewBox: string; width: number; height: number; startFrame: number; durFrames: number }> = ({ shapes, viewBox, width, height, startFrame, durFrames }) => {
  const frame = useCurrentFrame();
  const vbw = Number(viewBox.split(/\s+/)[2]) || 100;
  const outline = Math.max(1.5, vbw / 64); // viewBox-relative outline so the trace is visible at any scale
  const n = Math.max(1, shapes.length);
  const per = Math.max(1, durFrames) / n; // each shape gets an equal slice → drawn in sequence
  return (
    <div style={{ position: "absolute", left: 0, top: 0, width, height }}>
      <svg width={width} height={height} viewBox={viewBox} preserveAspectRatio="xMidYMid meet">
        {shapes.map((s, i) => {
          const ts = interpolate(frame, [startFrame + i * per, startFrame + (i + 1) * per], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
          if (ts <= 0) return null;
          const col = s.fill && s.fill !== "none" ? s.fill : "#1A1A1A";
          return (
            <path
              key={i}
              d={s.d}
              fill={col}
              fillOpacity={Math.max(0, Math.min(1, (ts - 0.5) * 2))}
              stroke={col}
              strokeWidth={(s.width as number) || outline}
              strokeLinecap="round"
              strokeLinejoin="round"
              pathLength={100}
              strokeDasharray={100}
              strokeDashoffset={(1 - ts) * 100}
            />
          );
        })}
      </svg>
    </div>
  );
};

const PlanElementView: React.FC<{ el: PlanElement; pack: Required<StylePack>; diagram?: boolean; index?: number }> = ({ el, pack, diagram, index }) => {
  const frame = useCurrentFrame();
  const { box, strokes, viewBox, draw, label } = el;
  if (frame < draw.startFrame) return null; // not yet revealed
  const left = box.x - box.w / 2;
  const top = box.y - box.h / 2;
  const n = Math.max(1, strokes.length);
  const per = draw.durFrames / n; // each stroke gets an equal slice of the element's draw window
  // Diagram card: icon sits centred in the upper-middle, label LOWER (lower third), with a clear
  // top + bottom margin — balanced, not top-heavy. iconTop pushes the whole icon group down.
  const iconH = box.h * (diagram ? 0.48 : 0.9);
  const iconTop = diagram ? box.h * 0.16 : 0;
  const labelTop = diagram ? box.h * 0.68 : box.h + 6;
  const roughBorder = diagram ? el._roughBorder : null; // baked wobbly card outline (rough style pass)
  const boxStyle = diagram
    ? {
        // rough pass draws its own wobbly border, so suppress the crisp CSS one
        border: roughBorder ? "3px solid transparent" : `3px solid ${pack.palette.accent}`,
        borderRadius: 24, background: `${pack.palette.accent}12`, boxShadow: "0 12px 34px rgba(15,23,42,0.10)",
      }
    : {};
  return (
    <div style={{ position: "absolute", left, top, width: box.w, height: box.h, boxSizing: "border-box", ...boxStyle }}>
      {diagram && typeof index === "number" ? (
        <div style={{
          position: "absolute", top: -20, left: -20, width: 48, height: 48, borderRadius: 999,
          background: pack.palette.accent, color: "#fff", display: "flex", alignItems: "center",
          justifyContent: "center", fontFamily: pack.font.label, fontWeight: 800, fontSize: 26,
          boxShadow: "0 4px 12px rgba(15,23,42,0.22)",
        }}>{index + 1}</div>
      ) : null}
      {el.chip ? (
        // color genre: soft colour chip behind the icon (icon stroke is the same colour)
        <div style={{
          position: "absolute", left: box.w * 0.16, top: iconTop + iconH * 0.02,
          width: box.w * 0.68, height: iconH * 0.96, borderRadius: 28, background: el.chip + "22",
          opacity: frame >= draw.startFrame ? 1 : 0,
        }} />
      ) : null}
      {roughBorder ? (
        <div style={{ position: "absolute", left: 0, top: 0, width: box.w, height: box.h }}>
          <SelfDrawSvg
            d={roughBorder} viewBox={`0 0 ${box.w} ${box.h}`} width={box.w} height={box.h}
            stroke={pack.palette.accent} strokeWidth={pack.stroke.width} hand={false}
            startFrame={draw.startFrame} durationInFrames={Math.max(6, draw.durFrames * 0.6)}
          />
        </div>
      ) : null}
      <div style={{ position: "absolute", left: 0, top: iconTop, width: box.w, height: iconH }}>
        {el.shapes && el.shapes.length ? (
          <FilledShapes shapes={el.shapes} viewBox={viewBox} width={box.w} height={iconH} startFrame={draw.startFrame} durFrames={draw.durFrames} />
        ) : null}
        {el.raster ? (
          <RasterRevealIllustration
            viewBox={el.maskViewBox || viewBox}
            raster={el.raster}
            strokes={el.maskStrokes || []}
            shapes={el.maskShapes || []}
            width={box.w}
            height={iconH}
            startFrame={draw.startFrame}
            durationInFrames={draw.durFrames}
            ink={pack.palette.ink}
            handBody="#33312E"
          />
        ) : (
          strokes.map((s, i) => (
            <div key={i} style={{ position: "absolute", left: 0, top: 0, width: box.w, height: iconH }}>
              <SelfDrawSvg
                d={s.d}
                viewBox={viewBox}
                width={box.w}
                height={iconH}
                stroke={s.stroke || INK}
                strokeWidth={s.width || 4}
                startFrame={draw.startFrame + i * per}
                durationInFrames={per}
                hand
              />
            </div>
          ))
        )}
      </div>
      {label ? <WriteOnText text={label} startFrame={draw.startFrame + draw.durFrames * 0.55} pack={pack} top={labelTop} /> : null}
    </div>
  );
};

const HighlightView: React.FC<{ ov: PlanOverlay; pack: Required<StylePack> }> = ({ ov, pack }) => {
  const frame = useCurrentFrame();
  if (frame < ov.startFrame) return null;
  const pad = 18;
  const w = ov.box.w + pad * 2;
  const h = ov.box.h + pad * 2;
  const left = ov.box.x - ov.box.w / 2 - pad;
  const top = ov.box.y - ov.box.h / 2 - pad;
  // a rough marker ellipse in a 0..100 viewBox
  const ellipse = "M50 6 a 44 44 0 1 0 0.1 0";
  return (
    <div style={{ position: "absolute", left, top, width: w, height: h }}>
      <SelfDrawSvg
        d={ellipse} viewBox="0 0 100 100" width={w} height={h}
        stroke={pack.palette.highlight} strokeWidth={pack.stroke.width + 1}
        startFrame={ov.startFrame} durationInFrames={ov.durFrames} hand={false}
      />
    </div>
  );
};

// Diagram-mode arrow between two element boxes, drawing on (no hand). Rendered over the canvas.
const Connector: React.FC<{ c: PlanConnector; canvas: { width: number; height: number }; pack: Required<StylePack> }> = ({ c, canvas, pack }) => {
  const frame = useCurrentFrame();
  if (frame < c.startFrame) return null;
  const ax = c.from.x, ay = c.from.y, bx = c.to.x, by = c.to.y;
  const dx = bx - ax, dy = by - ay, len = Math.hypot(dx, dy) || 1, ux = dx / len, uy = dy / len;
  // start/end at each box EDGE (+margin) so the arrow lives ONLY in the gap — never overlaps a box
  const horiz = Math.abs(ux) >= Math.abs(uy);
  const halfA = (horiz ? (c.from.w || 300) / 2 : (c.from.h || 300) / 2) + 18;
  const halfB = (horiz ? (c.to.w || 300) / 2 : (c.to.h || 300) / 2) + 18;
  const sx = ax + ux * halfA, sy = ay + uy * halfA, ex = bx - ux * halfB, ey = by - uy * halfB;
  const ah = 22;
  const lx = ex - ah * (ux - uy * 0.6), ly = ey - ah * (uy + ux * 0.6);
  const rx = ex - ah * (ux + uy * 0.6), ry = ey - ah * (uy - ux * 0.6);
  // rough style pass: swap the straight shaft for the baked wobbly path, keep a crisp arrowhead
  const shaft = c._roughShaft ? c._roughShaft : `M ${sx} ${sy} L ${ex} ${ey}`;
  const d = `${shaft} M ${ex} ${ey} L ${lx} ${ly} M ${ex} ${ey} L ${rx} ${ry}`;
  return (
    <div style={{ position: "absolute", left: 0, top: 0, width: canvas.width, height: canvas.height }}>
      <SelfDrawSvg
        d={d} viewBox={`0 0 ${canvas.width} ${canvas.height}`} width={canvas.width} height={canvas.height}
        stroke={pack.palette.accent} strokeWidth={pack.stroke.width} startFrame={c.startFrame} durationInFrames={c.durFrames} hand={false}
      />
    </div>
  );
};

export const WhiteboardPlanScene: React.FC<{ plan: ResolvedPlan }> = ({ plan }) => {
  const frame = useCurrentFrame();
  const transform = cameraTransform(plan.camera || [], frame, plan.canvas);
  const diagram = (plan.mode || "icons") === "diagram";
  // merge the plan's style pack over the defaults so partial packs still work
  const sp = plan.stylePack || {};
  const pack: Required<StylePack> = {
    board: sp.board || DEFAULT_PACK.board,
    palette: { ...DEFAULT_PACK.palette, ...(sp.palette || {}) },
    stroke: { ...DEFAULT_PACK.stroke, ...(sp.stroke || {}) },
    font: { ...DEFAULT_PACK.font, ...(sp.font || {}) },
  };
  return (
    <AbsoluteFill style={{ background: pack.board }}>
      <div
        style={{
          position: "absolute", left: 0, top: 0,
          width: plan.canvas.width, height: plan.canvas.height,
          transform, transformOrigin: "center center",
        }}
      >
        {diagram ? (plan.connectors || []).map((c, i) => <Connector key={`c${i}`} c={c} canvas={plan.canvas} pack={pack} />) : null}
        {(plan.elements || []).map((el, i) => <PlanElementView key={el.id} el={el} pack={pack} diagram={diagram} index={i} />)}
        {(plan.overlays || []).map((ov, i) => <HighlightView key={`ov${i}`} ov={ov} pack={pack} />)}
      </div>
    </AbsoluteFill>
  );
};

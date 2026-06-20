import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from "remotion";
import { SelfDrawSvg } from "./components/SelfDrawSvg";

// The "dumb" renderer for a RESOLVED whiteboard plan (produced by scripts/lib/plan/
// resolvePlan.mjs on the Node side). It only DRAWS what's already planned: per-element
// stroke draw-on (hand follows the pen), write-on labels, highlight overlays, and a
// camera transform. No resolving/layout here — that all happened upstream.

const INK = "#1F2937";
const BOARD = "#FBFBF7";
const ACCENT = "#F59E0B";

export interface PlanBox { x: number; y: number; w: number; h: number }
export interface PlanStroke { d: string; stroke?: string; width?: number }
export interface PlanElement {
  id: string;
  box: PlanBox;
  label: string | null;
  viewBox: string;
  strokes: PlanStroke[];
  draw: { startFrame: number; durFrames: number };
}
export interface PlanOverlay { kind: string; box: PlanBox; startFrame: number; durFrames: number }
export interface PlanCamera { type: string; scale: number; startFrame: number; endFrame: number; cx: number; cy: number }
export interface ResolvedPlan {
  fps: number;
  durationInFrames: number;
  canvas: { width: number; height: number };
  elements: PlanElement[];
  overlays: PlanOverlay[];
  camera: PlanCamera[];
}

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

const WriteOnText: React.FC<{ text: string; startFrame: number }> = ({ text, startFrame }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const charsPerSec = 16;
  const shown = Math.max(0, Math.floor(((frame - startFrame) / fps) * charsPerSec));
  return (
    <div
      style={{
        position: "absolute", bottom: -4, left: 0, width: "100%", textAlign: "center",
        fontFamily: "Inter, system-ui, sans-serif", fontWeight: 800, fontSize: 34,
        color: INK, opacity: frame >= startFrame ? 1 : 0, letterSpacing: "-0.01em",
      }}
    >
      {text.slice(0, shown)}
    </div>
  );
};

const PlanElementView: React.FC<{ el: PlanElement }> = ({ el }) => {
  const frame = useCurrentFrame();
  const { box, strokes, viewBox, draw, label } = el;
  if (frame < draw.startFrame) return null; // not yet revealed
  const left = box.x - box.w / 2;
  const top = box.y - box.h / 2;
  const n = Math.max(1, strokes.length);
  const per = draw.durFrames / n; // each stroke gets an equal slice of the element's draw window
  const iconH = box.h * 0.78;
  return (
    <div style={{ position: "absolute", left, top, width: box.w, height: box.h }}>
      {strokes.map((s, i) => (
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
      ))}
      {label ? <WriteOnText text={label} startFrame={draw.startFrame + draw.durFrames * 0.55} /> : null}
    </div>
  );
};

const HighlightView: React.FC<{ ov: PlanOverlay }> = ({ ov }) => {
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
        stroke={ACCENT} strokeWidth={5} startFrame={ov.startFrame} durationInFrames={ov.durFrames} hand={false}
      />
    </div>
  );
};

export const WhiteboardPlanScene: React.FC<{ plan: ResolvedPlan }> = ({ plan }) => {
  const frame = useCurrentFrame();
  const transform = cameraTransform(plan.camera || [], frame, plan.canvas);
  return (
    <AbsoluteFill style={{ background: BOARD }}>
      <div
        style={{
          position: "absolute", left: 0, top: 0,
          width: plan.canvas.width, height: plan.canvas.height,
          transform, transformOrigin: "center center",
        }}
      >
        {(plan.elements || []).map((el) => <PlanElementView key={el.id} el={el} />)}
        {(plan.overlays || []).map((ov, i) => <HighlightView key={`ov${i}`} ov={ov} />)}
      </div>
    </AbsoluteFill>
  );
};

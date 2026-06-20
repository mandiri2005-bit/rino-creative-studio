import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { Hand } from "./Hand";
import type { Shape, Stroke } from "../types";

// Detailed colour art animated whiteboard-style WITHOUT redrawing it: the REAL raster
// (original, full quality) is revealed through a self-drawing vector MASK. Pixels are
// never reconstructed, so the result looks exactly like the source.
//
// SYNC: the reveal and the hand are driven by ONE frontier. Every mask unit (filled
// region + outline stroke) is ordered into a reading-order SNAKE (banded top→bottom,
// alternating left→right / right→left) by its position, then revealed in that order.
// The hand rides the exact same cursor, so the artwork always appears right at the pen.
//
// NOTE: the alternative "single thick brush sweep" was tried (anti-pop, size-independent)
// but rolled back — on art without a dominant shape it read more wipe-y than this organic
// per-unit reveal. The per-unit reveal can pop a whole page if a single vectorized shape
// covers most of the canvas (e.g. a framed wedding invite); reach for the sweep there.
export const RasterRevealIllustration: React.FC<{
  viewBox: string;
  raster: string; // data URI or URL — the original artwork
  strokes?: Stroke[];
  shapes?: Shape[];
  width: number;
  height: number;
  startFrame: number;
  durationInFrames: number;
  ink: string;
  handBody: string;
}> = ({ viewBox, raster, strokes, shapes, width, height, startFrame, durationInFrames, ink, handBody }) => {
  const frame = useCurrentFrame();
  const reactId = React.useId();
  const maskId = "rr" + reactId.replace(/[^a-zA-Z0-9]/g, "");
  const [vx, vy, vw, vh] = viewBox.split(/\s+/).map(Number);
  const sx = width / (vw || 100);
  const sy = height / (vh || 100);
  const strokeArr = strokes || [];

  // representative point of a path = its first move-to (cheap, no DOM measuring).
  const repPoint = (d: string): { x: number; y: number } => {
    const m = /[Mm]\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/.exec(d);
    return m ? { x: +m[1], y: +m[2] } : { x: (vx || 0) + (vw || 0) / 2, y: (vy || 0) + (vh || 0) / 2 };
  };

  // vertical centre of each form (regex-sampled) → time its draw by WHERE it sits, so the scene
  // draws smoothly top→bottom and big shapes draw at their position (not dumping early).
  const cyOf = (d: string): number => {
    const nums = d.match(/-?\d*\.?\d+/g);
    if (!nums || nums.length < 2) return (vy || 0) + (vh || 100) / 2;
    let s = 0, c = 0;
    for (let i = 1; i < nums.length; i += 2) { s += parseFloat(nums[i]); c++; }
    return c ? s / c : (vy || 0) + (vh || 100) / 2;
  };
  type Unit = { x: number; y: number; cy: number; el: "shape" | "stroke"; d: string };
  const units: Unit[] = [
    ...(shapes || []).map((s) => ({ ...repPoint(s.d), cy: cyOf(s.d), el: "shape" as const, d: s.d })),
    ...strokeArr.map((s) => ({ ...repPoint(s.d), cy: cyOf(s.d), el: "stroke" as const, d: s.d })),
  ];

  const total = Math.max(1, durationInFrames);
  const tGlobal = interpolate(frame, [startFrame, startFrame + total], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const SPAN = 0.92; // reveal completes by 92% of the window, then a brief settle
  const WIN = 0.17;  // each region draws over this fraction of the window

  // NO mask units (vectorize unavailable, e.g. flux raster without potrace) → reveal the FULL
  // image with a left→right wipe so the (paid) raster is never lost or masked out to blank.
  if (units.length === 0) {
    const reveal = interpolate(tGlobal, [0, 0.92], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });
    return (
      <div style={{ position: "relative", width, height, overflow: "hidden" }}>
        <div style={{ width: `${Math.round(reveal * 100)}%`, height: "100%", overflow: "hidden" }}>
          <img src={raster} alt="" style={{ width, height, objectFit: "contain", maxWidth: "none" }} />
        </div>
      </div>
    );
  }

  // GOLPO-STYLE: the pen TRACES each form's ink outline (a visible line being drawn) THEN its colour
  // fills in — timed by the form's vertical centre so it draws top→bottom, NOT a curtain wipe.
  const startPof = (u: Unit) => Math.min(1, Math.max(0, (u.cy - (vy || 0)) / (vh || 100))) * (SPAN - WIN);
  const inkW = Math.max(1.6, (vw || 100) / 340);
  const frontY = (vy || 0) + Math.min(1, tGlobal / SPAN) * (vh || 100);
  let pen: Unit | null = null, bd = Infinity;
  for (const u of units) {
    const sp = Math.min(1, Math.max(0, (tGlobal - startPof(u)) / WIN));
    if (sp > 0.02 && sp < 0.98) { const dd = Math.abs(u.cy - frontY); if (dd < bd) { bd = dd; pen = u; } }
  }
  const handVisible = tGlobal > 0.005 && tGlobal < 0.985 && pen != null;

  return (
    <div style={{ position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={viewBox}>
        <defs>
          <mask id={maskId} maskUnits="userSpaceOnUse">
            {/* catch-up: solid fill trailing the front by ~12% so NO region is left empty/sparse —
                even pale/low-contrast areas potrace barely traces (e.g. a light koala). The potrace
                shapes give the organic LEADING edge; this fills solidly behind it. */}
            {(() => {
              const catchH = Math.max(0, Math.min(1, tGlobal / SPAN) - 0.12) * (vh || 100);
              return catchH > 0 ? <rect x={vx || 0} y={vy || 0} width={vw} height={catchH} fill="white" /> : null;
            })()}
            {units.map((u, i) => {
              const sp = Math.min(1, Math.max(0, (tGlobal - startPof(u)) / WIN));
              const fillOp = Math.min(1, Math.max(0, (sp - 0.5) / 0.5));
              return fillOp > 0 ? <path key={i} d={u.d} fill="white" opacity={fillOp} /> : null;
            })}
          </mask>
        </defs>
        <image
          href={raster}
          x={vx || 0}
          y={vy || 0}
          width={vw}
          height={vh}
          preserveAspectRatio="xMidYMid meet"
          mask={`url(#${maskId})`}
        />
        {/* ink outlines traced on top — the pen drawing the lines */}
        {units.map((u, i) => {
          const sp = Math.min(1, Math.max(0, (tGlobal - startPof(u)) / WIN));
          if (sp <= 0) return null;
          const traceP = Math.min(1, sp / 0.55);
          return (
            <path
              key={"ink" + i}
              d={u.d}
              fill="none"
              stroke={ink}
              strokeWidth={inkW}
              strokeLinecap="round"
              strokeLinejoin="round"
              pathLength={100}
              strokeDasharray={100}
              strokeDashoffset={(1 - traceP) * 100}
              opacity={0.82}
            />
          );
        })}
      </svg>
      {handVisible && pen ? (
        <Hand
          x={(pen.x - (vx || 0)) * sx}
          y={(frontY - (vy || 0)) * sy}
          size={Math.max(120, height * 0.5)}
          nib={ink}
          body={handBody}
        />
      ) : null}
    </div>
  );
};

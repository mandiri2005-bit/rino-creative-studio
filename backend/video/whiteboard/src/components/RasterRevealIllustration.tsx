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

  // units in DRAW ORDER — maskShapes are pre-ordered NEAREST-NEIGHBOUR by traceMaskB64, so the pen
  // moves continuously between adjacent forms (natural drawing, not a sweep). rep point = on the form.
  type Unit = { x: number; y: number; d: string };
  const units: Unit[] = [
    ...(shapes || []).map((s) => ({ ...repPoint(s.d), d: s.d })),
    ...strokeArr.map((s) => ({ ...repPoint(s.d), d: s.d })),
  ];

  const total = Math.max(1, durationInFrames);
  const tGlobal = interpolate(frame, [startFrame, startFrame + total], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const SPAN = 0.92;

  // NO mask units → reveal the FULL image with a left→right wipe so the raster is never blanked.
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

  // GOLPO-STYLE: forms drawn ONE AFTER ANOTHER along the nearest-neighbour path — for each, the pen
  // traces its ink outline then its colour fills. Each form's TIME ∝ its outline length (d-string
  // proxy) so big forms draw slower (natural) + even pacing. A catch-up band fills light regions.
  const inkW = Math.max(1.6, (vw || 100) / 340);
  const N = units.length;
  const lens = units.map((u) => Math.max(1, u.d.length));
  const totLen = lens.reduce((a, c) => a + c, 0) || 1;
  let cum = 0; const startF = lens.map((l) => { const s = (cum / totLen) * SPAN; cum += l; return s; });
  const durF = lens.map((l) => Math.max(0.012, (l / totLen) * SPAN) * 1.6);
  const spOf = (i: number) => Math.min(1, Math.max(0, (tGlobal - startF[i]) / durF[i]));
  let act = -1;
  for (let i = 0; i < N; i++) { const sp = spOf(i); if (sp > 0.01 && sp < 0.99) act = i; }
  const penU = act >= 0 ? units[act] : null;
  const handVisible = tGlobal > 0.005 && tGlobal < 0.985 && penU != null;

  return (
    <div style={{ position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={viewBox}>
        <defs>
          <mask id={maskId} maskUnits="userSpaceOnUse">
            {(() => {
              const catchH = Math.max(0, Math.min(1, tGlobal / SPAN) - 0.15) * (vh || 100);
              return catchH > 0 ? <rect x={vx || 0} y={vy || 0} width={vw} height={catchH} fill="white" /> : null;
            })()}
            {units.map((u, i) => {
              const op = Math.min(1, Math.max(0, (spOf(i) - 0.4) / 0.6));
              return op > 0 ? <path key={i} d={u.d} fill="white" opacity={op} /> : null;
            })}
          </mask>
        </defs>
        <image href={raster} x={vx || 0} y={vy || 0} width={vw} height={vh} preserveAspectRatio="xMidYMid meet" mask={`url(#${maskId})`} />
        {units.map((u, i) => {
          const sp = spOf(i);
          if (sp <= 0) return null;
          const traceP = Math.min(1, sp / 0.5);
          return (
            <path key={"ink" + i} d={u.d} fill="none" stroke={ink} strokeWidth={inkW} strokeLinecap="round" strokeLinejoin="round" pathLength={100} strokeDasharray={100} strokeDashoffset={(1 - traceP) * 100} opacity={0.82} />
          );
        })}
      </svg>
      {handVisible && penU ? (
        <Hand x={(penU.x - (vx || 0)) * sx} y={(penU.y - (vy || 0)) * sy} size={Math.max(120, height * 0.5)} nib={ink} body={handBody} />
      ) : null}
    </div>
  );
};

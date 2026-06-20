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
  const brush = Math.max(8, Math.round((vw || 100) / 34)); // thick reveal "pen"
  const strokeArr = strokes || [];

  // representative point of a path = its first move-to (cheap, no DOM measuring).
  const repPoint = (d: string): { x: number; y: number } => {
    const m = /[Mm]\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/.exec(d);
    return m ? { x: +m[1], y: +m[2] } : { x: (vx || 0) + (vw || 0) / 2, y: (vy || 0) + (vh || 0) / 2 };
  };

  // one ordered snake through every mask unit (regions first so lines land on top of them)
  const NBANDS = 4;
  const bandH = (vh || 100) / NBANDS;
  type Unit = { x: number; y: number; el: "shape" | "stroke"; d: string };
  const units: Unit[] = [
    ...(shapes || []).map((s) => ({ ...repPoint(s.d), el: "shape" as const, d: s.d })),
    ...strokeArr.map((s) => ({ ...repPoint(s.d), el: "stroke" as const, d: s.d })),
  ];
  units.sort((a, b) => {
    const ba = Math.min(NBANDS - 1, Math.max(0, Math.floor((a.y - (vy || 0)) / bandH)));
    const bb = Math.min(NBANDS - 1, Math.max(0, Math.floor((b.y - (vy || 0)) / bandH)));
    if (ba !== bb) return ba - bb; // top band first
    return ba % 2 === 0 ? a.x - b.x : b.x - a.x; // snake: even band L→R, odd band R→L
  });

  const N = Math.max(1, units.length);
  const total = Math.max(1, durationInFrames);
  const tGlobal = interpolate(frame, [startFrame, startFrame + total], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const SPAN = 0.92; // all units revealed by 92% of the window, then a brief settle
  const WIN = 0.06; // each unit fades in over this fraction (soft rolling frontier)

  // NO mask units (vectorize unavailable, e.g. flux raster without recraft) → reveal the FULL
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

  // hand cursor rides the same ordered snake at the reveal frontier
  const cursor = Math.min(1, tGlobal / SPAN) * (N - 1);
  const i0 = Math.floor(cursor);
  const i1 = Math.min(N - 1, i0 + 1);
  const f = cursor - i0;
  const hx = units[i0].x + (units[i1].x - units[i0].x) * f;
  const hy = units[i0].y + (units[i1].y - units[i0].y) * f;
  const handVisible = tGlobal > 0.005 && tGlobal < 0.985;

  return (
    <div style={{ position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={viewBox}>
        <defs>
          <mask id={maskId} maskUnits="userSpaceOnUse">
            {units.map((u, i) => {
              const center = (i / N) * SPAN;
              const op = interpolate(tGlobal, [center, center + WIN], [0, 1], {
                extrapolateLeft: "clamp",
                extrapolateRight: "clamp",
              });
              if (op <= 0) return null;
              return u.el === "shape" ? (
                <path key={i} d={u.d} fill="white" opacity={op} />
              ) : (
                <path
                  key={i}
                  d={u.d}
                  fill="none"
                  stroke="white"
                  strokeWidth={brush}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  opacity={op}
                />
              );
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
      </svg>
      {handVisible ? (
        <Hand
          x={(hx - (vx || 0)) * sx}
          y={(hy - (vy || 0)) * sy}
          size={Math.max(120, height * 0.5)}
          nib={ink}
          body={handBody}
        />
      ) : null}
    </div>
  );
};

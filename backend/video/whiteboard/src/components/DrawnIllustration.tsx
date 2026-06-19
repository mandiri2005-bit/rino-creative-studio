import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { lengthOf, pointAtProgress } from "../lib/path";
import { Hand } from "./Hand";
import type { Stroke } from "../types";

// Draws a multi-stroke vector illustration stroke-by-stroke: each stroke gets a slice
// of the total time proportional to its length (so a long contour takes longer than a
// short tick), earlier strokes stay fully drawn, and a single marker hand follows
// whichever stroke is currently being drawn. The strokes come from an LLM or an icon
// library — the engine doesn't care, it just needs the `d` paths.
export const DrawnIllustration: React.FC<{
  viewBox: string;
  strokes: Stroke[];
  width: number;
  height: number;
  startFrame: number;
  durationInFrames: number;
  defaultStroke: string;
  defaultWidth: number;
  handBody: string;
}> = ({
  viewBox,
  strokes,
  width,
  height,
  startFrame,
  durationInFrames,
  defaultStroke,
  defaultWidth,
  handBody,
}) => {
  const frame = useCurrentFrame();
  const [vx, vy, vw, vh] = viewBox.split(/\s+/).map(Number);
  const sx = width / (vw || 100);
  const sy = height / (vh || 100);

  const lens = strokes.map((s) => Math.max(1, lengthOf(s.d)));
  const totalLen = lens.reduce((a, b) => a + b, 0) || 1;
  const total = Math.max(1, durationInFrames);

  // proportional frame window per stroke
  let acc = 0;
  const windows = lens.map((l) => {
    const start = startFrame + (acc / totalLen) * total;
    acc += l;
    const end = startFrame + (acc / totalLen) * total;
    return { start, end };
  });

  // per-stroke draw fraction this frame; hand follows the first still-drawing stroke
  const ts = windows.map((w) =>
    interpolate(frame, [w.start, Math.max(w.start + 1, w.end)], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    })
  );
  const activeIdx = ts.findIndex((t) => t > 0.02 && t < 0.985);
  const handPt = activeIdx >= 0 ? pointAtProgress(strokes[activeIdx].d, ts[activeIdx]) : null;

  return (
    <div style={{ position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={viewBox} fill="none">
        {strokes.map((s, i) => (
          <path
            key={i}
            d={s.d}
            pathLength={100}
            stroke={s.stroke || defaultStroke}
            strokeWidth={s.width || defaultWidth}
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeDasharray={100}
            strokeDashoffset={(1 - ts[i]) * 100}
          />
        ))}
      </svg>
      {handPt ? (
        <Hand
          x={(handPt.x - (vx || 0)) * sx}
          y={(handPt.y - (vy || 0)) * sy}
          size={Math.max(120, height * 0.5)}
          nib={defaultStroke}
          body={handBody}
        />
      ) : null}
    </div>
  );
};

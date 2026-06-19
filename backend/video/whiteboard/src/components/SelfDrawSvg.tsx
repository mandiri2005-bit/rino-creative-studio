import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { pointAtProgress } from "../lib/path";
import { Hand } from "./Hand";

// The core "self-drawing" primitive: a stroke whose dash offset animates from the
// full (normalised) length down to 0, so the line appears to draw itself. A marker
// hand follows the live draw point (computed via getPointAtLength) and lifts off once
// the stroke is complete. `pathLength={100}` normalises the dash geometry.
export const SelfDrawSvg: React.FC<{
  d: string;
  viewBox: string;
  width: number;
  height: number;
  stroke: string;
  strokeWidth: number;
  startFrame: number;
  durationInFrames: number;
  hand?: boolean; // show the following marker (default true)
  handSize?: number;
  handBody?: string;
}> = ({
  d,
  viewBox,
  width,
  height,
  stroke,
  strokeWidth,
  startFrame,
  durationInFrames,
  hand = true,
  handSize,
  handBody = "#33312E",
}) => {
  const frame = useCurrentFrame();
  const t = interpolate(
    frame,
    [startFrame, startFrame + Math.max(1, durationInFrames)],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const offset = (1 - t) * 100;

  const [vx, vy, vw, vh] = viewBox.split(/\s+/).map(Number);
  const sx = width / (vw || 100);
  const sy = height / (vh || 100);
  const showHand = hand && t > 0.02 && t < 0.985;
  const pt = showHand ? pointAtProgress(d, t) : null;

  return (
    <div style={{ position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={viewBox} fill="none">
        <path
          d={d}
          pathLength={100}
          stroke={stroke}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeDasharray={100}
          strokeDashoffset={offset}
        />
      </svg>
      {pt ? (
        <Hand
          x={(pt.x - (vx || 0)) * sx}
          y={(pt.y - (vy || 0)) * sy}
          size={handSize ?? Math.max(90, height * 0.55)}
          nib={stroke}
          body={handBody}
        />
      ) : null}
    </div>
  );
};

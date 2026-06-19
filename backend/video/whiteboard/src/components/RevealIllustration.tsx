import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { Hand } from "./Hand";
import type { Shape } from "../types";

// Fill-REVEAL: render the full COLOURED illustration and wipe it in left-to-right, with
// the marker hand riding the wipe edge. The right technique for filled vector art
// (Recraft etc.) — stroke self-draw can only trace outlines, this shows the complete art.
export const RevealIllustration: React.FC<{
  viewBox: string;
  shapes: Shape[];
  width: number;
  height: number;
  startFrame: number;
  durationInFrames: number;
  handNib: string;
  handBody: string;
}> = ({ viewBox, shapes, width, height, startFrame, durationInFrames, handNib, handBody }) => {
  const frame = useCurrentFrame();
  const t = interpolate(frame, [startFrame, startFrame + Math.max(1, durationInFrames)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const clip = `inset(0 ${(1 - t) * 100}% 0 0)`; // reveal from the left
  const showHand = t > 0.02 && t < 0.98;
  return (
    <div style={{ position: "relative", width, height }}>
      <svg
        width={width}
        height={height}
        viewBox={viewBox}
        style={{ clipPath: clip, WebkitClipPath: clip }}
      >
        {shapes.map((s, i) => (
          <path
            key={i}
            d={s.d}
            fill={s.fill || "none"}
            stroke={s.stroke || "none"}
            strokeWidth={s.width || 0}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}
      </svg>
      {showHand ? (
        <Hand x={width * t} y={height * 0.52} size={Math.max(120, height * 0.42)} nib={handNib} body={handBody} />
      ) : null}
    </div>
  );
};

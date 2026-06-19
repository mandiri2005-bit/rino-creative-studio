import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { DrawnIllustration } from "./DrawnIllustration";
import { Hand } from "./Hand";
import type { Shape, Stroke } from "../types";

// The hand DRAWS the ink/colour outlines stroke-by-stroke across the duration, while the
// colour + labels appear GRADUALLY alongside (not a curtain wipe). Two colour sources:
//   svg    : raw SVG markup (incl <text>) — rendered as a layer that fades in gradually.
//            Used for diagrams (boxes/arrows/labels) and rich art that need their text.
//   shapes : parsed filled paths, faded in sequentially (document order).
export const DrawRevealIllustration: React.FC<{
  viewBox: string;
  strokes: Stroke[];
  shapes?: Shape[];
  svg?: string;
  width: number;
  height: number;
  startFrame: number;
  durationInFrames: number;
  ink: string;
  handBody: string;
}> = ({ viewBox, strokes, shapes, svg, width, height, startFrame, durationInFrames, ink, handBody }) => {
  const frame = useCurrentFrame();
  const [, , vw] = viewBox.split(/\s+/).map(Number);
  const sw = Math.max(2, Math.round((vw || 100) / 170));
  const t = interpolate(frame, [startFrame, startFrame + Math.max(1, durationInFrames)], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const n = Math.max(1, shapes?.length || 0);
  // size the raw SVG into the box (prepended width/height win over any existing attrs)
  const rawSized = svg
    ? svg.replace(/<svg/i, `<svg width="${width}" height="${height}" preserveAspectRatio="xMidYMid meet"`)
    : null;

  return (
    <div style={{ position: "relative", width, height }}>
      {rawSized ? (
        <div style={{ position: "absolute", inset: 0, opacity: t }} dangerouslySetInnerHTML={{ __html: rawSized }} />
      ) : (
        <svg width={width} height={height} viewBox={viewBox} style={{ position: "absolute", inset: 0 }}>
          {(shapes || []).map((s, i) => {
            const startP = (i / n) * 0.8;
            const op = interpolate(t, [startP, startP + 0.18], [0, 1], {
              extrapolateLeft: "clamp",
              extrapolateRight: "clamp",
            });
            return (
              <path
                key={i}
                d={s.d}
                fill={s.fill || "none"}
                stroke={s.stroke || "none"}
                strokeWidth={s.width || 0}
                opacity={op}
                strokeLinejoin="round"
                strokeLinecap="round"
              />
            );
          })}
        </svg>
      )}
      {/* the hand traces the outlines over the full duration, on top */}
      <div style={{ position: "absolute", inset: 0 }}>
        <DrawnIllustration
          viewBox={viewBox}
          strokes={strokes}
          width={width}
          height={height}
          startFrame={startFrame}
          durationInFrames={durationInFrames}
          defaultStroke={ink}
          defaultWidth={sw}
          handBody={handBody}
        />
      </div>
      {/* detailed/inverted art has no strokes to trace → a hand sweeps as it reveals */}
      {svg && strokes.length === 0 && t > 0.02 && t < 0.98 ? (
        <Hand x={width * t} y={height * 0.52} size={Math.max(120, height * 0.42)} nib={ink} body={handBody} />
      ) : null}
    </div>
  );
};

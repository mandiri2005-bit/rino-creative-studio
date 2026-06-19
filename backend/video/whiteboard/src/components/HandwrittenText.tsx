import React from "react";
import { useCurrentFrame } from "remotion";
import { Hand } from "./Hand";

// The drawing hand trails the writing head: an inline relative wrapper of zero width is
// placed right after the visible text, and the Hand's pen tip sits at its origin (the
// writing head). Sized to the handwriting so it reads on the board.
const WritingHand: React.FC<{ size: number; nib: string; body: string }> = ({ size, nib, body }) => (
  <span style={{ position: "relative", width: 0, height: 0, display: "inline-block", alignSelf: "flex-end" }}>
    <Hand x={0} y={-size * 0.06} size={size} nib={nib} body={body} />
  </span>
);

// Reveals text character-by-character across all lines, paced by `framesPerChar`.
// Completed lines show in full; the active line shows its written portion with the
// pen at the head; not-yet-reached lines reserve their height so layout never jumps.
// `align` controls horizontal placement (flex-start | center).
export const HandwrittenText: React.FC<{
  lines: string[];
  startFrame: number;
  framesPerChar: number;
  fontFamily: string;
  ink: string;
  penColor: string;
  fontSize: number;
  align?: "flex-start" | "center";
  markerBody?: string;
}> = ({
  lines,
  startFrame,
  framesPerChar,
  fontFamily,
  ink,
  penColor,
  fontSize,
  align = "flex-start",
  markerBody = "#33312E",
}) => {
  const frame = useCurrentFrame();
  const elapsed = Math.max(0, frame - startFrame);
  const revealed = Math.floor(elapsed / framesPerChar); // total chars shown so far

  return (
    <div
      style={{
        fontFamily,
        color: ink,
        fontSize,
        fontWeight: 700,
        lineHeight: 1.25,
        width: "100%",
      }}
    >
      {lines.map((line, i) => {
        const before = lines.slice(0, i).reduce((a, l) => a + l.length, 0);
        const visible = Math.min(line.length, Math.max(0, revealed - before));
        const isActive = revealed > before && visible < line.length;
        return (
          <div
            key={i}
            style={{
              display: "flex",
              alignItems: "flex-end",
              justifyContent: align,
              minHeight: fontSize * 1.3,
              whiteSpace: "pre",
            }}
          >
            <span>{line.slice(0, visible)}</span>
            {isActive ? <WritingHand size={fontSize * 1.6} nib={penColor} body={markerBody} /> : null}
          </div>
        );
      })}
    </div>
  );
};

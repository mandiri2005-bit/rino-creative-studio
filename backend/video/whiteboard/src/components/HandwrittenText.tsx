import React from "react";
import { useCurrentFrame } from "remotion";

// A marker/pen sprite that trails the writing head (inline, sits after the text).
const Pen: React.FC<{ color: string; size: number }> = ({ color, size }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    style={{ marginLeft: 4, transform: "rotate(8deg)", alignSelf: "flex-end" }}
  >
    <path d="M3 21l3.2-1L18 8.2 15.8 6 4 17.8 3 21z" fill={color} />
    <path
      d="M16.6 7.4l-2.2-2.2 1.7-1.7a1.7 1.7 0 0 1 2.4 0l.5.5a1.7 1.7 0 0 1 0 2.4l-2.4 1z"
      fill={color}
      opacity="0.85"
    />
  </svg>
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
}> = ({
  lines,
  startFrame,
  framesPerChar,
  fontFamily,
  ink,
  penColor,
  fontSize,
  align = "flex-start",
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
            {isActive ? <Pen color={penColor} size={fontSize * 0.78} /> : null}
          </div>
        );
      })}
    </div>
  );
};

import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { Hand } from "./Hand";
import { lengthOf, pointAtProgress } from "../lib/path";
import type { DiagramItem } from "../types";

// A whiteboard DIAGRAM: the hand DRAWS each shape (stroke self-draw) and WRITES each
// label (char-by-char) in document order, one item at a time. No fades, nothing appears
// before the hand reaches it — fixes "the hand only draws boxes, doesn't write" and
// "the box shows before it's drawn".
const CHARW = 0.52; // rough glyph width as a fraction of font size (for the writing head)

export const DrawnDiagram: React.FC<{
  viewBox: string;
  items: DiagramItem[];
  width: number;
  height: number;
  startFrame: number;
  durationInFrames: number;
  fontFamily: string;
  ink: string;
  handBody: string;
}> = ({ viewBox, items, width, height, startFrame, durationInFrames, fontFamily, ink, handBody }) => {
  const frame = useCurrentFrame();
  const [, , vwRaw, vhRaw] = viewBox.split(/\s+/).map(Number);
  const vw = vwRaw || 100;
  const vh = vhRaw || 100;
  const sx = width / vw;
  const sy = height / vh;

  // weight each item by how long it takes to draw/write, then give it a time window
  const weights = items.map((it) =>
    it.kind === "text" ? Math.max(10, it.text.length * 7) : Math.max(24, lengthOf(it.d))
  );
  const total = weights.reduce((a, b) => a + b, 0) || 1;
  let acc = 0;
  const win = weights.map((w) => {
    const s = acc / total;
    acc += w;
    return { s, e: acc / total };
  });
  const tg = (frame - startFrame) / Math.max(1, durationInFrames);

  const prog = (i: number) =>
    interpolate(tg, [win[i].s, win[i].e], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" });

  const activeIdx = items.findIndex((_, i) => {
    const p = prog(i);
    return p > 0.02 && p < 0.98;
  });

  // hand position = head of the active item (stroke tip, or text writing head)
  let hand: { x: number; y: number } | null = null;
  if (activeIdx >= 0) {
    const it = items[activeIdx];
    const p = prog(activeIdx);
    if (it.kind === "stroke") {
      const pt = pointAtProgress(it.d, p);
      if (pt) hand = { x: pt.x * sx, y: pt.y * sy };
    } else {
      const fs = it.fontSize ?? 40;
      const w = it.text.length * fs * CHARW;
      const left = it.anchor === "middle" ? it.x - w / 2 : it.anchor === "end" ? it.x - w : it.x;
      const headX = left + Math.floor(p * it.text.length) * fs * CHARW;
      hand = { x: headX * sx, y: (it.y - fs * 0.3) * sy };
    }
  }

  return (
    <div style={{ position: "relative", width, height }}>
      <svg width={width} height={height} viewBox={viewBox} style={{ position: "absolute", inset: 0 }}>
        {items.map((it, i) =>
          it.kind === "stroke" ? (
            <path
              key={i}
              d={it.d}
              fill="none"
              stroke={it.stroke || ink}
              strokeWidth={it.width || 4}
              strokeLinecap="round"
              strokeLinejoin="round"
              pathLength={100}
              strokeDasharray={100}
              strokeDashoffset={(1 - prog(i)) * 100}
            />
          ) : null
        )}
      </svg>

      {items.map((it, i) => {
        if (it.kind !== "text") return null;
        const fs = it.fontSize ?? 40;
        const vis = Math.floor(prog(i) * it.text.length);
        if (vis <= 0) return null;
        return (
          <div
            key={`t${i}`}
            style={{
              position: "absolute",
              left: it.x * sx,
              top: (it.y - fs) * sy,
              fontFamily,
              fontSize: fs * sy,
              lineHeight: 1,
              color: it.fill || ink,
              whiteSpace: "pre",
              transform:
                it.anchor === "middle" ? "translateX(-50%)" : it.anchor === "end" ? "translateX(-100%)" : "none",
            }}
          >
            {it.text.slice(0, vis)}
          </div>
        );
      })}

      {hand ? (
        <Hand x={hand.x} y={hand.y} size={Math.max(120, height * 0.42)} nib={ink} body={handBody} />
      ) : null}
    </div>
  );
};

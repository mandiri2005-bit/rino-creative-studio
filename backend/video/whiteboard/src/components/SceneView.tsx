import React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import type { Theme } from "../theme";
import type { Illustration, Layout } from "../types";
import { ILLUSTRATION_DRAW_S, DRAW_REVEAL_S, DIAGRAM_DRAW_S, RASTER_REVEAL_S } from "../timing";
import { lucideToIllustration } from "../lib/lucide";
import { HandwrittenText } from "./HandwrittenText";
import { SelfDrawSvg } from "./SelfDrawSvg";
import { DrawnIllustration } from "./DrawnIllustration";
import { RevealIllustration } from "./RevealIllustration";
import { DrawRevealIllustration } from "./DrawRevealIllustration";
import { RasterRevealIllustration } from "./RasterRevealIllustration";
import { DrawnDiagram } from "./DrawnDiagram";
import {
  ICONS,
  UNDERLINE_H,
  UNDERLINE_PATH,
  UNDERLINE_VIEWBOX,
  UNDERLINE_W,
} from "./icons";

interface Props {
  lines: string[];
  layout: Layout;
  framesPerChar: number;
  writeFrames: number;
  accent: boolean;
  icon: string | null;
  lucide: string | null;
  illustration: Illustration | null;
  fontFamily: string;
  theme: Theme;
  fontSize: number;
  fps: number;
}

// One scene, composed per layout. Text writes first (startFrame 0); underlines, icons
// and illustrations self-draw after (decorStart). Graphic precedence: explicit
// `illustration` > `lucide` (name/concept -> Lucide strokes) > built-in `icon`.
export const SceneView: React.FC<Props> = ({
  lines,
  layout,
  framesPerChar,
  writeFrames,
  accent,
  icon,
  lucide,
  illustration,
  fontFamily,
  theme,
  fontSize,
  fps,
}) => {
  const { width: frameW, height: frameH } = useVideoConfig();
  const decorStart = writeFrames;
  const iconDef = icon ? ICONS[icon] : null;
  const illo: Illustration | null = illustration ?? (lucide ? lucideToIllustration(lucide) : null);
  const lucideMode = !illustration && !!illo; // resolved from a Lucide name → icon-style sizing
  const wide = !!illustration; // custom illustrations are wide scenes; lucide/icons are compact
  const hasGraphic = Boolean(illo) || Boolean(iconDef);

  const text = (align: "flex-start" | "center", size = fontSize, lns = lines, start = 0) => (
    <HandwrittenText
      lines={lns}
      startFrame={start}
      framesPerChar={framesPerChar}
      fontFamily={fontFamily}
      ink={theme.ink}
      penColor={theme.accent}
      fontSize={size}
      align={align}
      markerBody={theme.markerBody}
    />
  );

  const underline = (align: "flex-start" | "center") => (
    <div style={{ display: "flex", justifyContent: align, width: "100%", marginTop: 14 }}>
      <SelfDrawSvg
        d={UNDERLINE_PATH}
        viewBox={UNDERLINE_VIEWBOX}
        width={UNDERLINE_W}
        height={UNDERLINE_H}
        stroke={theme.accent}
        strokeWidth={7}
        startFrame={decorStart}
        durationInFrames={Math.round(fps * 0.8)}
        handSize={Math.round(fontSize * 1.5)}
        handBody={theme.markerBody}
      />
    </div>
  );

  // graphic = illustration/lucide if present, else the built-in icon.
  const graphic = (targetW: number, start: number) => {
    if (illo) {
      const [, , vw, vh] = illo.viewBox.split(/\s+/).map(Number);
      const w = targetW;
      const h = Math.round((w * (vh || 100)) / (vw || 100));
      if (illo.mode === "diagram" && illo.items?.length) {
        return (
          <DrawnDiagram
            viewBox={illo.viewBox}
            items={illo.items}
            width={w}
            height={h}
            startFrame={start}
            durationInFrames={Math.round(fps * DIAGRAM_DRAW_S)}
            fontFamily={fontFamily}
            ink={theme.ink}
            handBody={theme.markerBody}
          />
        );
      }
      if (illo.mode === "raster-reveal" && illo.raster && illo.strokes?.length) {
        return (
          <RasterRevealIllustration
            viewBox={illo.viewBox}
            raster={illo.raster}
            strokes={illo.strokes}
            shapes={illo.shapes}
            width={w}
            height={h}
            startFrame={start}
            durationInFrames={Math.round(fps * RASTER_REVEAL_S)}
            ink={theme.ink}
            handBody={theme.markerBody}
          />
        );
      }
      if (illo.mode === "draw-reveal" && (illo.shapes?.length || illo.svg) && illo.strokes?.length) {
        return (
          <DrawRevealIllustration
            viewBox={illo.viewBox}
            strokes={illo.strokes}
            shapes={illo.shapes}
            svg={illo.svg}
            width={w}
            height={h}
            startFrame={start}
            durationInFrames={Math.round(fps * DRAW_REVEAL_S)}
            ink={theme.ink}
            handBody={theme.markerBody}
          />
        );
      }
      if (illo.mode === "reveal" && illo.shapes?.length) {
        return (
          <RevealIllustration
            viewBox={illo.viewBox}
            shapes={illo.shapes}
            width={w}
            height={h}
            startFrame={start}
            durationInFrames={Math.round(fps * ILLUSTRATION_DRAW_S)}
            handNib={theme.ink}
            handBody={theme.markerBody}
          />
        );
      }
      return (
        <DrawnIllustration
          viewBox={illo.viewBox}
          strokes={illo.strokes || []}
          width={w}
          height={h}
          startFrame={start}
          durationInFrames={Math.round(fps * ILLUSTRATION_DRAW_S)}
          defaultStroke={lucideMode ? theme.sub : theme.ink}
          defaultWidth={lucideMode ? 1.6 : 4}
          handBody={theme.markerBody}
        />
      );
    }
    if (iconDef) {
      return (
        <SelfDrawSvg
          d={iconDef.d}
          viewBox={iconDef.viewBox}
          width={targetW}
          height={targetW}
          stroke={theme.sub}
          strokeWidth={5}
          startFrame={start}
          durationInFrames={Math.round(fps * 1)}
          handSize={Math.round(targetW * 0.7)}
          handBody={theme.markerBody}
        />
      );
    }
    return null;
  };

  // full = the illustration fills the frame (crisp, not cramped) — for standalone art.
  if (layout === "full" && illo) {
    const [, , vw, vh] = illo.viewBox.split(/\s+/).map(Number);
    const aspect = (vw || 1) / (vh || 1);
    // Colour art (draw-reveal) and raster detail COVER the frame edge-to-edge: size the
    // box so both dims reach the frame, then clip the overflow — no board-coloured margin
    // or "frame" around the picture. Diagrams stay CONTAINED (meet) so labels aren't cropped.
    const cover = illo.mode === "draw-reveal" || illo.mode === "raster-reveal";
    const targetW = cover
      ? Math.ceil(Math.max(frameW, frameH * aspect))
      : Math.round(Math.min(frameW * 0.9, frameH * 0.88 * aspect));
    return (
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", overflow: "hidden" }}>
        {graphic(targetW, decorStart)}
      </AbsoluteFill>
    );
  }

  if (layout === "bullets") {
    return (
      <AbsoluteFill style={{ padding: "8% 9%", justifyContent: "center", alignItems: "flex-start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: fontSize * 0.35 }}>
          {lines.map((line, i) => {
            const before = lines.slice(0, i).reduce((a, l) => a + l.length, 0);
            const start = before * framesPerChar;
            const checkSize = Math.round(fontSize * 0.95);
            return (
              <div key={i} style={{ display: "flex", alignItems: "flex-end", gap: fontSize * 0.25 }}>
                <div style={{ flexShrink: 0, marginBottom: fontSize * 0.12 }}>
                  <SelfDrawSvg
                    d={ICONS.check.d}
                    viewBox={ICONS.check.viewBox}
                    width={checkSize}
                    height={checkSize}
                    stroke={theme.accent}
                    strokeWidth={7}
                    startFrame={start}
                    durationInFrames={Math.round(fps * 0.4)}
                    hand={false}
                  />
                </div>
                {text("flex-start", fontSize, [line], start)}
              </div>
            );
          })}
        </div>
      </AbsoluteFill>
    );
  }

  if (layout === "split") {
    const gW = illo ? (wide ? Math.round(fontSize * 8) : Math.round(fontSize * 3.2)) : Math.round(fontSize * 4);
    return (
      <AbsoluteFill style={{ flexDirection: "row", alignItems: "center", padding: "8% 7%", gap: "4%" }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          {text("flex-start")}
          {accent ? underline("flex-start") : null}
        </div>
        <div style={{ flex: 1, display: "flex", justifyContent: "center", alignItems: "center" }}>
          {graphic(gW, decorStart)}
        </div>
      </AbsoluteFill>
    );
  }

  if (layout === "center" || layout === "title") {
    const size = layout === "title" ? Math.round(fontSize * 1.35) : fontSize;
    const gW = illo ? (wide ? Math.round(fontSize * 7) : Math.round(fontSize * 2.4)) : Math.round(fontSize * 2.2);
    return (
      <AbsoluteFill
        style={{ padding: "8% 10%", justifyContent: "center", alignItems: "center", textAlign: "center" }}
      >
        {hasGraphic ? (
          <div style={{ marginBottom: fontSize * 0.4, display: "flex", justifyContent: "center" }}>
            {graphic(gW, decorStart)}
          </div>
        ) : null}
        {text("center", size)}
        {accent ? underline("center") : null}
      </AbsoluteFill>
    );
  }

  // left (default)
  const gWleft = illo ? (wide ? Math.round(fontSize * 6) : Math.round(fontSize * 2.6)) : iconDef ? iconDef.size : 0;
  return (
    <AbsoluteFill style={{ padding: "8% 9%", justifyContent: "center", alignItems: "flex-start" }}>
      {text("flex-start")}
      {accent ? underline("flex-start") : null}
      {hasGraphic ? (
        <div
          style={
            wide
              ? { position: "absolute", right: "6%", top: "50%", transform: "translateY(-50%)" }
              : { position: "absolute", right: "9%", top: "16%" }
          }
        >
          {graphic(gWleft, decorStart)}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

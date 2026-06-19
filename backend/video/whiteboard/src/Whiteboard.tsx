import React from "react";
import { AbsoluteFill, Audio, Sequence, staticFile, useVideoConfig } from "remotion";
import { loadFont } from "@remotion/google-fonts/Caveat";
import type { Spec } from "./types";
import { resolveTheme } from "./theme";
import { planScenes } from "./timing";
import { SceneView } from "./components/SceneView";
import { setHandImage } from "./components/Hand";

const { fontFamily } = loadFont();

const src = (s: string) => (/^https?:\/\//.test(s) ? s : staticFile(s));

export const Whiteboard: React.FC<Spec> = (spec) => {
  const { fps } = useVideoConfig();
  const theme = resolveTheme(spec.theme);
  const fontSize = spec.fontSize ?? 88;
  const plan = planScenes(spec, fps);
  setHandImage(spec.handImage ? src(spec.handImage) : null);
  const showGrid = spec.grid !== false;

  return (
    <AbsoluteFill
      style={{
        backgroundColor: spec.background || theme.bg,
        ...(showGrid
          ? {
              backgroundImage: `linear-gradient(${theme.grid} 1px, transparent 1px), linear-gradient(90deg, ${theme.grid} 1px, transparent 1px)`,
              backgroundSize: "64px 64px",
            }
          : {}),
      }}
    >
      {spec.voiceover ? <Audio src={src(spec.voiceover)} /> : null}

      {plan.scenes.map((s, i) => (
        <Sequence key={i} from={s.start} durationInFrames={s.duration}>
          <SceneView
            lines={s.lines}
            layout={s.layout}
            framesPerChar={plan.framesPerChar}
            writeFrames={s.writeFrames}
            accent={s.accent}
            icon={s.icon}
            lucide={s.lucide}
            illustration={s.illustration}
            fontFamily={fontFamily}
            theme={theme}
            fontSize={fontSize}
            fps={fps}
          />
          {s.audioSrc ? <Audio src={src(s.audioSrc)} /> : null}
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};

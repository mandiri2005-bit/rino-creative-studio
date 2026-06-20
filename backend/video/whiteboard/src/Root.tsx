import React from "react";
import { Composition } from "remotion";
import { Whiteboard } from "./Whiteboard";
import { computeMeta } from "./timing";
import type { Spec } from "./types";
import { WhiteboardPlanScene, type ResolvedPlan } from "./WhiteboardPlan";

// Plan-driven composition (Golpo-like). Props = a RESOLVED plan from
// backend/video/whiteboard/plan/resolvePlan.mjs; metadata come from the plan.
const emptyPlan: ResolvedPlan = {
  fps: 30, durationInFrames: 30, canvas: { width: 1920, height: 1080 },
  elements: [], overlays: [], camera: [],
};

// Default props so Remotion Studio opens with something playable. The real props
// come from input/*.json at render time (--props=... or scripts/render.mjs).
const defaultProps: Spec = {
  theme: "marker",
  fps: 30,
  width: 1920,
  height: 1080,
  fontSize: 88,
  writeSecondsPerChar: 0.05,
  voiceover: null,
  scenes: [
    { layout: "title", lines: ["ceritaAI Whiteboard"], icon: "bulb", holdSeconds: 1.0 },
    {
      layout: "left",
      lines: ["Tulisan muncul,", "huruf demi huruf."],
      accent: { type: "underline", line: 1 },
      holdSeconds: 1.2,
    },
    {
      layout: "bullets",
      lines: ["Tulis teks otomatis", "Ikon menggambar sendiri", "Tangan mengikuti garis"],
      holdSeconds: 1.4,
    },
    { layout: "split", lines: ["Render jadi MP4,", "siap publish."], icon: "rocket", holdSeconds: 1.4 },
    { layout: "center", lines: ["Semua dalam", "Bahasa Indonesia."], icon: "globe", holdSeconds: 1.5 },
  ],
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="Whiteboard"
        component={Whiteboard}
        durationInFrames={1}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={defaultProps}
        calculateMetadata={({ props }) => computeMeta(props as Spec)}
      />
      <Composition
        id="WhiteboardPlan"
        component={WhiteboardPlanScene}
        durationInFrames={30}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={{ plan: emptyPlan }}
        calculateMetadata={({ props }) => {
          const plan = (props as { plan: ResolvedPlan }).plan;
          return {
            durationInFrames: Math.max(1, plan.durationInFrames || 30),
            fps: plan.fps || 30,
            width: plan.canvas?.width || 1920,
            height: plan.canvas?.height || 1080,
          };
        }}
      />
    </>
  );
};

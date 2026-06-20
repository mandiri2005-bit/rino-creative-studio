import React from "react";
import { AbsoluteFill, Sequence, Audio, staticFile } from "remotion";
import { WhiteboardPlanScene, type ResolvedPlan } from "./WhiteboardPlan";

// Multi-scene whiteboard video: sequences N RESOLVED plans (one per narration scene),
// each WhiteboardPlanScene playing in its own <Sequence> with its per-scene narration
// <Audio>. Whole-video, one render pass (mirrors the proven renderWhiteboard path).
// Props are produced Node-side by render.mjs (resolvePlan per scene + audio copy to public).

export interface PlanScene {
  plan: ResolvedPlan;
  audioSrc?: string | null; // relative path under the render publicDir (staticFile)
}
export interface PlanVideoSpec {
  fps?: number;
  width?: number;
  height?: number;
  scenes: PlanScene[];
}

export const planVideoFrames = (spec: PlanVideoSpec): number =>
  Math.max(1, (spec.scenes || []).reduce((a, s) => a + Math.max(1, s.plan?.durationInFrames || 1), 0));

export const WhiteboardPlanVideo: React.FC<PlanVideoSpec> = ({ scenes }) => {
  let offset = 0;
  return (
    <AbsoluteFill style={{ background: "#FBFBF7" }}>
      {(scenes || []).map((s, i) => {
        const dur = Math.max(1, s.plan?.durationInFrames || 1);
        const from = offset;
        offset += dur;
        return (
          <Sequence key={i} from={from} durationInFrames={dur}>
            <WhiteboardPlanScene plan={s.plan} />
            {s.audioSrc ? <Audio src={staticFile(s.audioSrc)} /> : null}
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

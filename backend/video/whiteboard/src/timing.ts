import type { Illustration, Layout, Spec } from "./types";

// Pure timing math. Turns a Spec into a per-scene timeline (start / duration in
// frames) plus the writing speed. Same function powers calculateMetadata (total
// duration) and the Whiteboard component (per-scene sequencing), so they never drift.

export interface PlannedScene {
  lines: string[];
  layout: Layout;
  start: number; // frame the scene begins, relative to the whole video
  duration: number; // total frames for the scene
  writeFrames: number; // frames spent writing all the text
  accent: boolean;
  accentLine: number; // which line the underline sits under
  icon: string | null;
  lucide: string | null;
  illustration: Illustration | null;
  audioSrc: string | null;
}

export interface Plan {
  framesPerChar: number;
  scenes: PlannedScene[];
  total: number;
}

const DEFAULT_WRITE_S_PER_CHAR = 0.05;
const DEFAULT_HOLD_S = 1.0;
const ACCENT_DRAW_S = 1.0; // time for an underline / icon to self-draw
export const ILLUSTRATION_DRAW_S = 2.5; // time for a multi-stroke illustration to draw
export const DRAW_REVEAL_S = 4.0; // draw-reveal: outline-sketch phase + colour-sweep phase
export const DIAGRAM_DRAW_S = 6.0; // diagram: draw each shape + write each label in sequence
export const RASTER_REVEAL_S = 5.0; // raster-reveal: uncover the real artwork along the drawn mask

export const fpsOf = (spec: Spec): number => spec.fps ?? 30;

export function planScenes(spec: Spec, fps: number): Plan {
  const framesPerChar = Math.max(
    1,
    Math.round((spec.writeSecondsPerChar ?? DEFAULT_WRITE_S_PER_CHAR) * fps)
  );
  const accentFrames = Math.round(ACCENT_DRAW_S * fps);

  let cursor = 0;
  const scenes: PlannedScene[] = [];
  for (const sc of spec.scenes ?? []) {
    const chars = sc.lines.join("").length;
    const writeFrames = Math.max(framesPerChar, chars * framesPerChar);
    const holdFrames = Math.round((sc.holdSeconds ?? DEFAULT_HOLD_S) * fps);
    const drawSeconds =
      sc.illustration?.mode === "diagram"
        ? DIAGRAM_DRAW_S
        : sc.illustration?.mode === "raster-reveal"
          ? RASTER_REVEAL_S
          : sc.illustration?.mode === "draw-reveal"
            ? DRAW_REVEAL_S
            : ILLUSTRATION_DRAW_S;
    const drawFrames =
      sc.illustration || sc.lucide
        ? Math.round(drawSeconds * fps)
        : sc.accent || sc.icon
          ? accentFrames
          : 0;

    let duration = writeFrames + drawFrames + holdFrames;
    if (sc.durationSeconds && sc.durationSeconds > 0) {
      duration = Math.round(sc.durationSeconds * fps);
    }

    scenes.push({
      lines: sc.lines,
      layout: sc.layout ?? "left",
      start: cursor,
      duration: Math.max(1, duration),
      writeFrames,
      accent: Boolean(sc.accent),
      accentLine:
        sc.accent?.line != null ? sc.accent.line : sc.lines.length - 1,
      icon: sc.icon ?? null,
      lucide: sc.lucide ?? null,
      illustration: sc.illustration ?? null,
      audioSrc: sc.audioSrc ?? null,
    });
    cursor += Math.max(1, duration);
  }

  return { framesPerChar, scenes, total: Math.max(1, cursor) };
}

export function computeMeta(spec: Spec) {
  const fps = fpsOf(spec);
  const width = spec.width ?? 1920;
  const height = spec.height ?? 1080;
  const { total } = planScenes(spec, fps);
  return { durationInFrames: total, fps, width, height };
}

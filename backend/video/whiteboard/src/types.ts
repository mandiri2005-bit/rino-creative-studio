// The input contract. This is exactly the JSON a future "one-click" backend would
// produce (script -> scenes) and hand to the render engine. Keep it small and stable.

export type ThemeName = "marker" | "chalkboard";

// How a scene is composed. left = text left (default); center = centered;
// title = big centered headline; split = text left + big drawn icon right;
// bullets = each line gets its own self-drawing check as it's written.
export type Layout = "left" | "center" | "title" | "split" | "bullets" | "full";

// Icon name = any key in ICONS (see components/icons.ts). Kept as string so the
// JSON spec isn't over-constrained; unknown names simply render no icon.
export type IconName = string;

export interface Accent {
  // a hand-drawn swoosh under a written line (self-draws after the line is written)
  type: "underline";
  line?: number; // which line (0-indexed) the underline sits under; default = last line
}

// A multi-stroke vector illustration drawn stroke-by-stroke (hand follows each).
// `strokes` is what an LLM emits (or an icon-library lookup returns) — NOT a raster.
// Each stroke can carry its own colour/width; otherwise the theme defaults apply.
export type Stroke = { d: string; stroke?: string; width?: number };
export type Shape = { d: string; fill?: string; stroke?: string; width?: number };
// A diagram element in document order: a drawn shape OR a written label.
export type DiagramItem =
  | { kind: "stroke"; d: string; stroke?: string; width?: number }
  | { kind: "text"; x: number; y: number; text: string; fill?: string; fontSize?: number; anchor?: string };
// mode "draw" (default): stroke self-draw (line-art). mode "reveal": the full COLOURED
// art is shown and wiped in (for filled AI vector art that can't be stroke-drawn).
export type Illustration = {
  viewBox: string;
  // draw = stroke self-draw; reveal = full-colour wipe; draw-reveal = hand draws the ink
  // outlines while the colour fades in gradually alongside.
  // diagram = the hand DRAWS shapes and WRITES labels in order (no fades).
  mode?: "draw" | "reveal" | "draw-reveal" | "diagram" | "raster-reveal";
  strokes?: Stroke[];
  shapes?: Shape[];
  items?: DiagramItem[]; // for mode "diagram"
  // raster-reveal: the ORIGINAL raster (data URI / public path) is revealed through a
  // self-drawing vector mask (strokes+shapes) — pixels stay original quality, no redraw.
  raster?: string;
  // raw SVG markup (incl <text> labels + fills) used as the colour/detail layer in
  // draw-reveal — lets diagrams (boxes/arrows/labels) and rich art reveal with their text.
  svg?: string;
};

export interface Scene {
  lines: string[]; // the ON-SCREEN text, written one line at a time, char by char
  narration?: string; // the SPOKEN text for this scene (TTS source); falls back to lines
  layout?: Layout; // default "left"
  accent?: Accent; // optional self-drawing underline
  icon?: IconName; // optional self-drawing icon (placement depends on layout)
  lucide?: string; // optional Lucide icon name OR concept (auto-resolved to strokes)
  illustration?: Illustration; // optional multi-stroke vector art (takes precedence over icon)
  holdSeconds?: number; // pause after everything is drawn (default 1.0)
  durationSeconds?: number; // hard override for the whole scene (e.g. to match narration)
  audioSrc?: string | null; // optional per-scene narration (path under public/ or a URL)
}

// `type` (not `interface`) so it carries an implicit index signature and is
// assignable to Remotion's `Record<string, unknown>` Composition props bound.
export type Spec = {
  theme: ThemeName;
  fps?: number; // default 30
  width?: number; // default 1920
  height?: number; // default 1080
  fontSize?: number; // handwriting size in px at the chosen height (default 88)
  writeSecondsPerChar?: number; // writing speed (default 0.05s/char ~= 20 chars/s)
  voiceover?: string | null; // optional single narration track for the whole video
  background?: string; // board colour override (e.g. "#9C9C9C" for a grey board)
  grid?: boolean; // faint grid on the board (default true; false = plain board like a slide)
  handImage?: string; // optional PNG/photo hand (path under public/ or URL); else the built-in hand
  tts?: { provider?: "gemini" | "openai"; model?: string; voice?: string }; // per-job TTS choice
  scenes: Scene[];
}

// whiteboard_visual_plan contract constants (guide §D/§E). The plan is renderer-
// independent: it says WHAT to draw, in which semantic slot, and WHEN (beats) — never
// pixel coordinates and never Remotion specifics. Validator + resolver read these.

export const ALLOWED_ELEMENT_TYPES = ["icon", "text", "arrow", "box", "shape"];

// Beat actions (guide §E). draw/write/emphasis act on a `target` element; camera +
// scene-transition actions may target an element id or "full_canvas".
export const ALLOWED_ACTIONS = [
  "draw_icon", "write_text", "draw_arrow", "draw_box", "draw_circle",
  "highlight_circle", "underline", "marker_sweep",
  "pan_to", "zoom_to", "zoom_out", "fade_old", "erase", "transform",
];

// Actions whose `target` must be an existing element id (others may target full_canvas).
export const ELEMENT_TARGET_ACTIONS = new Set([
  "draw_icon", "write_text", "draw_box", "draw_circle", "highlight_circle", "underline", "fade_old", "transform",
]);

export const MAX_LABEL_WORDS = 5;        // labels stay short + punchy (guide §D)
export const DEFAULT_CANVAS = { width: 1920, height: 1080 };
export const DEFAULT_FPS = 30;

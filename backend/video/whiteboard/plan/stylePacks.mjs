// Style packs (guide §O) — keep visuals consistent video-to-video. A pack sets the board,
// ink/accent palette, stroke weight, and label font. The plan only names a pack
// (plan.style_pack); resolvePlan attaches the resolved object so the renderer is "dumb".
// Pure data + one resolver → unit-testable.

export const STYLE_PACKS = {
  clean_explainer: {
    name: "clean_explainer",
    board: "#FBFBF7",
    palette: { ink: "#1F2937", muted: "#64748B", accent: "#2563EB", highlight: "#F59E0B", success: "#10B981", warning: "#EF4444" },
    stroke: { width: 4, linecap: "round", linejoin: "round" },
    font: { label: "Inter, system-ui, sans-serif", weight: 800, labelSize: 34 },
  },
  chalkboard: {
    name: "chalkboard",
    board: "#16302A",
    palette: { ink: "#F2F2EC", muted: "#A7C4BC", accent: "#7FD1B9", highlight: "#F4C95D", success: "#8FD694", warning: "#E89B7C" },
    stroke: { width: 4, linecap: "round", linejoin: "round" },
    font: { label: "Inter, system-ui, sans-serif", weight: 800, labelSize: 34 },
  },
  bold_marker: {
    name: "bold_marker",
    board: "#FFFFFF",
    palette: { ink: "#111827", muted: "#6B7280", accent: "#DB2777", highlight: "#F59E0B", success: "#059669", warning: "#DC2626" },
    stroke: { width: 6, linecap: "round", linejoin: "round" },
    font: { label: "Inter, system-ui, sans-serif", weight: 900, labelSize: 38 },
  },
};

export const STYLE_PACK_NAMES = Object.keys(STYLE_PACKS);

export function resolveStylePack(name) {
  return STYLE_PACKS[name] || STYLE_PACKS.clean_explainer;
}

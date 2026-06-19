import type { ThemeName } from "./types";

// Two board looks. `marker` = light whiteboard, `chalkboard` = dark board.
// ink = main handwriting, accent = pen/underline, sub = secondary icons,
// markerBody = barrel colour of the drawing-hand marker (must read on the board).
export interface Theme {
  bg: string;
  ink: string;
  accent: string;
  sub: string;
  grid: string;
  markerBody: string;
}

export const THEMES: Record<ThemeName, Theme> = {
  marker: {
    bg: "#FBFBF7",
    ink: "#1A1A1A",
    accent: "#D85A30",
    sub: "#534AB7",
    grid: "rgba(0,0,0,0.035)",
    markerBody: "#33312E",
  },
  chalkboard: {
    bg: "#16302A",
    ink: "#F2F2EC",
    accent: "#7FD8B6",
    sub: "#FFD56B",
    grid: "rgba(255,255,255,0.05)",
    markerBody: "#E8E8E2",
  },
};

export const resolveTheme = (name: ThemeName | undefined): Theme =>
  THEMES[name as ThemeName] ?? THEMES.marker;

// Remotion-side animation helpers (guide §K). Frame-based: given the current frame and a
// beat {start,end} in seconds, return eased 0..1 progress. Kept tiny + pure so the
// composition stays "dumb" — all planning/resolving happens on the Node side.

export interface Beat {
  start: number;
  end: number;
}

export function beatProgress(frame: number, fps: number, beat: Beat): number {
  const t = frame / fps;
  const duration = Math.max(0.001, beat.end - beat.start);
  const raw = (t - beat.start) / duration;
  return Math.min(1, Math.max(0, raw));
}

export function easeOutCubic(t: number): number {
  return 1 - Math.pow(1 - t, 3);
}

export function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

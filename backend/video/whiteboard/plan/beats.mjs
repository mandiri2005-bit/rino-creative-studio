// Beat timing helpers (guide §F). When the Visual Director gives a beat COUNT but not
// explicit timings (or to fill gaps), distribute evenly across the VO-measured duration.
// When word-level TTS timestamps exist, prefer those upstream. Pure → unit-testable.

export function distributeBeats(durationSeconds, beatCount, minBeat = 0.8) {
  const n = Math.max(1, Math.floor(beatCount));
  const usable = Math.max(durationSeconds, n * minBeat);
  const base = usable / n;
  const beats = [];
  let cursor = 0;
  for (let i = 0; i < n; i++) {
    const start = cursor;
    const end = i === n - 1 ? durationSeconds : Math.min(durationSeconds, cursor + base);
    beats.push({ start: Number(start.toFixed(2)), end: Number(end.toFixed(2)) });
    cursor = end;
  }
  return beats;
}

export function secondsToFrames(seconds, fps) {
  return Math.max(0, Math.round(Number(seconds || 0) * fps));
}

// The beat that DRAWS/WRITES an element (first draw_icon/write_text targeting it). If an
// element has no explicit draw beat, it draws at t=0 over a short default window.
export function drawBeatFor(elementId, beats, fallbackEnd = 1.5) {
  const b = (beats || []).find(
    (x) => (x.action === "draw_icon" || x.action === "write_text") && x.target === elementId
  );
  return b || { start: 0, end: Math.min(fallbackEnd, 1.5), action: "draw_icon", target: elementId };
}

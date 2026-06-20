// renderers/manim.mjs — Manim backend runner (Guide-2 §J). SCAFFOLD / EXPERIMENTAL, NOT wired.
//
// Renders a resolved scene whose geometry.source === "manim" by running a CONTROLLED Manim
// template (python/whiteboard/manim_generators/<generator>_scene.py) with the scene's data. The
// LLM only supplies data, never Manim code (roadmap §J). Requires manim + LaTeX/Cairo in the
// image — the worker does NOT ship these, so the dispatcher keeps Remotion default until proven.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, readdirSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const GEN_DIR = "python/whiteboard/manim_generators";
const SCENE_CLASS = { linear_equation: "GeneratedLinearEquationScene" };

export async function renderManimScene(resolvedScene, { quality = "-ql" } = {}) {
  const generator = resolvedScene.geometry?.generator || "linear_equation";
  const cls = SCENE_CLASS[generator];
  if (!cls) throw new Error(`manim: unknown generator "${generator}"`);
  const dir = mkdtempSync(join(tmpdir(), "wbmanim-"));
  const dataPath = join(dir, "data.json");
  writeFileSync(dataPath, JSON.stringify(resolvedScene.geometry?.data || {}, null, 2));
  const pyFile = join(GEN_DIR, `${generator}_scene.py`);
  await new Promise((res, rej) => {
    const p = spawn("manim", [quality, pyFile, cls, "--format", "mp4", "--media_dir", dir, dataPath], { stdio: "inherit" });
    p.on("error", rej); p.on("close", (c) => (c === 0 ? res() : rej(new Error(`manim exit ${c} (manim + LaTeX must be installed)`))));
  });
  // manim writes the mp4 somewhere under media_dir/videos/**; find the newest .mp4
  const out = findNewestMp4(dir);
  if (!out) throw new Error("manim: no mp4 produced");
  return { path: out, renderer: "manim", sceneId: resolvedScene.scene_id };
}

function findNewestMp4(root) {
  let best = null, bestT = -1;
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name), s = statSync(p);
      if (s.isDirectory()) walk(p);
      else if (name.endsWith(".mp4") && s.mtimeMs > bestT) { best = p; bestT = s.mtimeMs; }
    }
  };
  try { walk(root); } catch { /* ignore */ }
  return best;
}

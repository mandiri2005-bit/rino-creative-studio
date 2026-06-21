// ─────────────────────────────────────────────────────────────────────────────
// video/params.mjs — pure dispatch-planning logic (no I/O, fully unit-testable).
//
// The Python scene segmenter (python/video_segmenter.py) is the source of truth
// for HOW MANY scenes a video has and what each one says. This Node module owns
// the orchestration counterpart: given a scene count, how does the engine
// dispatch it — all at once, or in rate-safe batches — and what does it cost.
//
// These constants MUST stay in lockstep with video_segmenter.py:
//   BATCH_SIZE = 10            (full-parallel ≤ 10 scenes, batched above)
//   TIER credits = fast×2 / hd×5 / hd+×8 per scene
// Both suites assert the SAME canonical preset rows by hand — tests/node/
// video_params.test.mjs here and tests/python/test_video_segmenter.py on the
// Python side — so drift in either fails its own suite. (There is no single
// cross-process test that diffs the two; the shared literals are the contract.)
// ─────────────────────────────────────────────────────────────────────────────

export const BATCH_SIZE = 10;

// DISPATCH batch size — an ORCHESTRATION concern, decoupled from BATCH_SIZE (which stays the
// UI card-threshold + Python cost lockstep). Batches dispatch SEQUENTIALLY (each waits for its
// slowest scene), so small batches serialize asset-gen for no reason: the per-queue concurrency
// (VIDEO_*_CONCURRENCY, default 10) + per-resource semaphores (clip submit cap, encoder slots)
// already throttle PEAK load, so a bigger dispatch batch does NOT raise peak concurrency — it
// only removes the inter-batch stalls. Default 24 → a typical ≤24-scene video dispatches as ONE
// full-parallel batch (the proven ≤10 path, just wider). Env-tunable; lower to 10 to revert.
export const DISPATCH_BATCH_SIZE = Math.max(1, Number(process.env.VIDEO_BATCH_SIZE) || 24);

// Per-scene planning credits by quality tier (the up-front hold estimate; the
// real charge is metered per scene at dispatch). Keys match the Python module.
export const TIER_CREDITS_PER_SCENE = Object.freeze({ fast: 2, hd: 5, hd_plus: 8 });

const TIER_ALIASES = {
  fast: "fast",
  hd: "hd",
  "hd+": "hd_plus", hdplus: "hd_plus", hd_plus: "hd_plus", hdp: "hd_plus",
};

export function normalizeTier(tier) {
  const t = String(tier || "hd").trim().toLowerCase().replace(/\s+/g, "_");
  return TIER_ALIASES[t] || (TIER_CREDITS_PER_SCENE[t] ? t : "hd");
}

/** "full_parallel" when the whole video fits one batch, else "batch". */
export function dispatchMode(sceneCount, batchSize = DISPATCH_BATCH_SIZE) {
  return sceneCount <= batchSize ? "full_parallel" : "batch";
}

/** How many scenes per dispatch batch, e.g. 43 → [10,10,10,10,3]. */
export function batchPlan(sceneCount, batchSize = DISPATCH_BATCH_SIZE) {
  const n = Math.max(0, Math.trunc(sceneCount));
  if (n === 0) return [];
  if (n <= batchSize) return [n];
  const plan = [];
  let left = n;
  while (left > 0) {
    plan.push(Math.min(batchSize, left));
    left -= batchSize;
  }
  return plan;
}

/** Credits to hold up front for a whole video of `sceneCount` scenes at `tier`. */
export function creditsForScenes(sceneCount, tier = "hd") {
  return Math.max(0, Math.trunc(sceneCount)) * TIER_CREDITS_PER_SCENE[normalizeTier(tier)];
}

/** Credits for every tier — handy for the UI picker. */
export function creditsByTier(sceneCount) {
  const n = Math.max(0, Math.trunc(sceneCount));
  return Object.fromEntries(
    Object.entries(TIER_CREDITS_PER_SCENE).map(([t, r]) => [t, n * r])
  );
}

/** "cards" for a small video, "bar" once scene cards become unwieldy (>10). */
export function progressUi(sceneCount, cardLimit = BATCH_SIZE) {
  return sceneCount <= cardLimit ? "cards" : "bar";
}

/**
 * Turn a scene list into the ordered batches the orchestrator dispatches.
 * Returns an array of arrays of scene objects (or indices), preserving order.
 */
export function intoBatches(scenes, batchSize = DISPATCH_BATCH_SIZE) {
  const out = [];
  for (let i = 0; i < scenes.length; i += batchSize) {
    out.push(scenes.slice(i, i + batchSize));
  }
  return out;
}

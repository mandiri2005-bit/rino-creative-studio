/**
 * Node.js unit tests — backend/video/params.mjs (dispatch planning).
 * Run: node --test tests/node/video_params.test.mjs
 *
 * The dispatch + credit table here MUST match the Python segmenter contract
 * (python/video_segmenter.py --all-durations). The canonical preset rows are
 * asserted below so the two languages can never silently drift.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  BATCH_SIZE, TIER_CREDITS_PER_SCENE, normalizeTier, dispatchMode, batchPlan,
  creditsForScenes, creditsByTier, progressUi, intoBatches,
} from "../../backend/video/params.mjs";

// scene counts straight from the Duration Presets contract (0.5→15 min)
const PRESET_SCENES = { "30s": 2, "1m": 3, "2m": 6, "3m": 9, "5m": 14, "10m": 29, "15m": 43 };

describe("batchPlan", () => {
  it("returns one batch when full-parallel", () => {
    assert.deepEqual(batchPlan(2), [2]);
    assert.deepEqual(batchPlan(9), [9]);
    assert.deepEqual(batchPlan(10), [10]);
  });
  it("splits into rate-safe batches above 10", () => {
    assert.deepEqual(batchPlan(14), [10, 4]);
    assert.deepEqual(batchPlan(29), [10, 10, 9]);
    assert.deepEqual(batchPlan(43), [10, 10, 10, 10, 3]);
  });
  it("sums back to the scene count", () => {
    for (const n of Object.values(PRESET_SCENES)) {
      assert.equal(batchPlan(n).reduce((a, b) => a + b, 0), n);
    }
  });
  it("handles zero", () => {
    assert.deepEqual(batchPlan(0), []);
  });
});

describe("dispatchMode", () => {
  it("full_parallel up to and including the batch size", () => {
    assert.equal(dispatchMode(9), "full_parallel");
    assert.equal(dispatchMode(10), "full_parallel");
  });
  it("batch above the batch size", () => {
    assert.equal(dispatchMode(14), "batch");
    assert.equal(dispatchMode(43), "batch");
  });
});

describe("progressUi", () => {
  it("cards small, bar large", () => {
    assert.equal(progressUi(9), "cards");
    assert.equal(progressUi(10), "cards");
    assert.equal(progressUi(14), "bar");
  });
});

describe("normalizeTier", () => {
  it("maps aliases", () => {
    assert.equal(normalizeTier("hd+"), "hd_plus");
    assert.equal(normalizeTier("HD Plus"), "hd_plus");
    assert.equal(normalizeTier("fast"), "fast");
    assert.equal(normalizeTier("garbage"), "hd");
  });
});

describe("credits", () => {
  it("per-scene rates are 2/5/8", () => {
    assert.deepEqual(TIER_CREDITS_PER_SCENE, { fast: 2, hd: 5, hd_plus: 8 });
  });
  it("matches the preset credit table (formula, not the doc's 1-min typo)", () => {
    const expect = {
      2:  { fast: 4,  hd: 10,  hd_plus: 16 },
      3:  { fast: 6,  hd: 15,  hd_plus: 24 },  // doc renders 18/28 — typo
      6:  { fast: 12, hd: 30,  hd_plus: 48 },
      9:  { fast: 18, hd: 45,  hd_plus: 72 },
      14: { fast: 28, hd: 70,  hd_plus: 112 },
      29: { fast: 58, hd: 145, hd_plus: 232 },
      43: { fast: 86, hd: 215, hd_plus: 344 },
    };
    for (const [scenes, row] of Object.entries(expect)) {
      assert.deepEqual(creditsByTier(Number(scenes)), row, `scenes=${scenes}`);
    }
  });
  it("creditsForScenes picks the tier", () => {
    assert.equal(creditsForScenes(6, "fast"), 12);
    assert.equal(creditsForScenes(6, "hd"), 30);
    assert.equal(creditsForScenes(6, "hd+"), 48);
  });
});

describe("intoBatches", () => {
  it("chunks a scene list preserving order", () => {
    const scenes = Array.from({ length: 14 }, (_, i) => i + 1);
    const batches = intoBatches(scenes);
    assert.equal(batches.length, 2);
    assert.deepEqual(batches[0], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    assert.deepEqual(batches[1], [11, 12, 13, 14]);
  });
});

describe("BATCH_SIZE", () => {
  it("is 10 (the Veo/Kling concurrency ceiling)", () => {
    assert.equal(BATCH_SIZE, 10);
  });
});

/**
 * Node.js unit tests — backend/video/orchestrator.mjs decision logic (pure).
 * Run: node --test tests/node/video_orchestrator.test.mjs
 *
 * These assert the fan-out/fan-in brain: when to dispatch the next batch, when
 * to stitch, when to fail. No Redis, no BullMQ — pure planAdvance/batchRanges.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { batchRanges, planAdvance, progressOf } from "../../backend/video/orchestrator.mjs";

// scene-state factories
const done = () => ({ audioStatus: "done", visualStatus: "done" });
const fallback = () => ({ audioStatus: "done", visualStatus: "fallback" });
const pending = () => ({ audioStatus: "pending", visualStatus: "pending" });
const halfA = () => ({ audioStatus: "done", visualStatus: "pending" });
const failed = () => ({ audioStatus: "failed", visualStatus: "pending" });
const mk = (n, f) => Array.from({ length: n }, f);

describe("batchRanges", () => {
  it("maps a batch plan to scene ranges", () => {
    assert.deepEqual(batchRanges([10, 4]), [[0, 10], [10, 14]]);
    assert.deepEqual(batchRanges([9]), [[0, 9]]);
    assert.deepEqual(batchRanges([10, 10, 10, 10, 3]), [
      [0, 10], [10, 20], [20, 30], [30, 40], [40, 43],
    ]);
  });
});

describe("planAdvance — full-parallel (≤10 scenes, one batch)", () => {
  const batchPlan = [6];
  it("waits while scenes are in flight", () => {
    const scenes = [done(), done(), halfA(), pending(), pending(), pending()];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0] }), { action: "wait" });
  });
  it("stitches once every scene is complete (clip fallback counts)", () => {
    const scenes = [done(), fallback(), done(), fallback(), done(), done()];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0] }), { action: "stitch" });
  });
  it("fails fast on a hard scene failure", () => {
    const scenes = [done(), failed(), pending(), pending(), pending(), pending()];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0] }), { action: "fail" });
  });
  it("treats a null/missing scene as a hard failure (no infinite wait)", () => {
    const scenes = [done(), null, done(), done(), done(), done()];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0] }), { action: "fail" });
  });
});

describe("planAdvance — batched (>10 scenes)", () => {
  const batchPlan = [10, 4]; // 14 scenes
  it("dispatches batch 1 once batch 0 is fully complete", () => {
    const scenes = [...mk(10, done), ...mk(4, pending)];
    assert.deepEqual(
      planAdvance({ batchPlan, scenes, dispatched: [0] }),
      { action: "dispatch", batchIndex: 1 }
    );
  });
  it("waits if batch 0 is not yet complete", () => {
    const scenes = [...mk(9, done), halfA(), ...mk(4, pending)];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0] }), { action: "wait" });
  });
  it("does not redispatch a batch already dispatched (waits for it)", () => {
    const scenes = [...mk(10, done), ...mk(4, pending)];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0, 1] }), { action: "wait" });
  });
  it("stitches when both batches complete", () => {
    const scenes = mk(14, done);
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0, 1] }), { action: "stitch" });
  });
});

describe("planAdvance — long form (43 scenes, 5 batches)", () => {
  const batchPlan = [10, 10, 10, 10, 3];
  it("advances one batch at a time", () => {
    // batches 0,1 done & dispatched; batch 2 in flight → wait
    const scenes = [...mk(20, done), ...mk(23, pending)];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0, 1] }), { action: "dispatch", batchIndex: 2 });
  });
  it("reaches stitch only at full completion", () => {
    const scenes = [...mk(40, done), ...mk(3, pending)];
    assert.deepEqual(planAdvance({ batchPlan, scenes, dispatched: [0, 1, 2, 3, 4] }), { action: "wait" });
    assert.deepEqual(planAdvance({ batchPlan, scenes: mk(43, done), dispatched: [0, 1, 2, 3, 4] }), { action: "stitch" });
  });
});

describe("progressOf", () => {
  it("reports the completion percentage", () => {
    assert.equal(progressOf(mk(4, done)), 100);
    assert.equal(progressOf([done(), done(), pending(), pending()]), 50);
    assert.equal(progressOf([]), 0);
  });
});

/**
 * Node.js unit tests — backend/video/store.mjs pure helpers.
 * Run: node --test tests/node/video_store.test.mjs
 *
 * These cover the parts that DON'T touch Redis: scene-kind mapping (the
 * snake_case/camelCase boundary the midpoint review flagged) and the
 * completeness/failure predicates the orchestrator's planAdvance reads.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { sceneKind, sceneComplete, sceneFailed } from "../../backend/video/store.mjs";

describe("sceneKind — honors the Python segmenter's snake_case", () => {
  it("snake_case clip_eligible:true → clip (the bug the review caught)", () => {
    assert.equal(sceneKind({ clip_eligible: true }), "clip");
    assert.equal(sceneKind({ clip_eligible: false }), "image");
  });
  it("camelCase clipEligible also works", () => {
    assert.equal(sceneKind({ clipEligible: true }), "clip");
  });
  it("explicit kind wins", () => {
    assert.equal(sceneKind({ kind: "image", clip_eligible: true }), "image");
    assert.equal(sceneKind({ kind: "clip" }), "clip");
  });
  it("defaults to image when nothing is set", () => {
    assert.equal(sceneKind({}), "image");
  });
});

describe("sceneComplete / sceneFailed predicates", () => {
  it("complete needs audio done/fallback + visual done/fallback", () => {
    assert.equal(sceneComplete({ audioStatus: "done", visualStatus: "done" }), true);
    assert.equal(sceneComplete({ audioStatus: "done", visualStatus: "fallback" }), true);
    // audio "fallback" = silent track (TTS failed) → still counts complete, job not killed
    assert.equal(sceneComplete({ audioStatus: "fallback", visualStatus: "done" }), true);
    assert.equal(sceneComplete({ audioStatus: "fallback", visualStatus: "fallback" }), true);
    assert.equal(sceneComplete({ audioStatus: "done", visualStatus: "pending" }), false);
    assert.equal(sceneComplete({ audioStatus: "pending", visualStatus: "done" }), false);
  });
  it("null scene is neither complete nor failed (handled by planAdvance)", () => {
    assert.equal(!!sceneComplete(null), false);
    assert.equal(!!sceneFailed(null), false);
  });
  it("failed if either side failed", () => {
    assert.equal(sceneFailed({ audioStatus: "failed", visualStatus: "pending" }), true);
    assert.equal(sceneFailed({ audioStatus: "done", visualStatus: "failed" }), true);
    assert.equal(sceneFailed({ audioStatus: "done", visualStatus: "done" }), false);
  });
});

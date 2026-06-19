/**
 * Node.js unit tests — backend/video/workers.mjs (the bits that are pure/importable).
 * Run: node --test tests/node/video_workers.test.mjs
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { IMAGE_FALLBACK_MODELS } from "../../backend/video/workers.mjs";

describe("IMAGE_FALLBACK_MODELS — aspect-correct provider before the squares", () => {
  it("tries an aspect-honouring model (flux-kontext-pro) FIRST", () => {
    // flux-kontext-pro honours the requested 16:9; nano-banana is 1:1-only and
    // seedream-4-0 is hardcoded 2K-square → a square the cover-crop chops. So the
    // aspect-correct provider must come before either square one.
    const flux = IMAGE_FALLBACK_MODELS.indexOf("flux-kontext-pro");
    const nano = IMAGE_FALLBACK_MODELS.indexOf("nano-banana");
    const seed = IMAGE_FALLBACK_MODELS.indexOf("seedream-4-0");
    assert.ok(flux > -1, "flux-kontext-pro present");
    assert.equal(IMAGE_FALLBACK_MODELS[0], "flux-kontext-pro");
    if (nano > -1) assert.ok(flux < nano, "flux before nano-banana (1:1)");
    if (seed > -1) assert.ok(flux < seed, "flux before seedream (2K square)");
  });
});

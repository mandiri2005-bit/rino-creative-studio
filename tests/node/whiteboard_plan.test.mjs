// Whiteboard plan-engine tests inside rino-creative-studio (ported from the standalone).
// Pure Node, no Remotion/node_modules. Run: node --test tests/node/whiteboard_plan.test.mjs
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const WB = join(ROOT, "backend", "video", "whiteboard");
const ASSETS = join(WB, "assets", "whiteboard");
const SAMPLE = join(WB, "plan_sample.json");

const { WHITEBOARD_TEMPLATES, TEMPLATE_NAMES } = await import(join(WB, "plan", "templates.mjs"));
const { SLOT_MAP_16_9, slotBox, layoutWhiteboardPlan } = await import(join(WB, "plan", "slots.mjs"));
const { loadManifest, resolveAsset } = await import(join(WB, "plan", "resolver.mjs"));
const { validateWhiteboardPlan } = await import(join(WB, "plan", "validate.mjs"));
const { secondsToFrames, drawBeatFor } = await import(join(WB, "plan", "beats.mjs"));
const { resolvePlan } = await import(join(WB, "plan", "resolvePlan.mjs"));
const { generateWhiteboardVisualPlan, extractJson } = await import(join(WB, "plan", "visualDirector.mjs"));

const GOOD = readFileSync(SAMPLE, "utf8");

describe("templates / slots", () => {
  it("5 templates; every allowedSlot resolves to a box", () => {
    assert.equal(TEMPLATE_NAMES.length, 5);
    for (const [t, def] of Object.entries(WHITEBOARD_TEMPLATES))
      for (const s of def.allowedSlots) assert.ok(slotBox(s), `${t}:${s}`);
  });
  it("layout attaches box / throws on unknown slot", () => {
    assert.deepEqual(layoutWhiteboardPlan({ elements: [{ id: "a", slot: "left_center" }] }).elements[0].box, SLOT_MAP_16_9.left_center);
    assert.throws(() => layoutWhiteboardPlan({ elements: [{ id: "x", slot: "ghost" }] }), /Unknown slot/);
  });
});

describe("resolver", () => {
  const man = loadManifest(ASSETS);
  it("known query → asset; unknown → generic fallback", () => {
    assert.equal(resolveAsset("tired office worker", man).asset.id, "office_worker");
    assert.equal(resolveAsset("zzz", man).fallback, true);
  });
});

describe("validator", () => {
  const plan = JSON.parse(GOOD);
  it("accepts sample; rejects broken", () => {
    assert.equal(validateWhiteboardPlan(plan).ok, true);
    const bad = JSON.parse(GOOD);
    bad.beats.push({ start: 0, end: 99, action: "nope", target: "ghost" });
    assert.equal(validateWhiteboardPlan(bad).ok, false);
  });
});

describe("resolvePlan (no Remotion)", () => {
  const rp = resolvePlan(SAMPLE, { assetsDir: ASSETS, fps: 30 });
  it("VO duration → frames; assets resolved; overlays + camera mapped", () => {
    assert.equal(rp.durationInFrames, 261);
    assert.equal(rp.elements.length, 4);
    assert.ok(rp.elements.every((e) => !e.fallback && e.strokes.length > 0 && e.box));
    assert.equal(rp.overlays.length, 1);
    assert.equal(rp.camera.length, 3);
  });
});

describe("visual director (mock LLM)", () => {
  const scene = { scene_id: "scene_001", narration_text: "x", duration_actual: 8.7 };
  it("good reply → valid plan; duration forced to VO", async () => {
    const r = await generateWhiteboardVisualPlan(scene, { callLLM: async () => "```json\n" + GOOD + "\n```" });
    assert.equal(r.attempts, 1);
    assert.equal(r.plan.duration, 8.7);
  });
  it("repairs after a bad reply", async () => {
    let n = 0;
    const r = await generateWhiteboardVisualPlan(scene, { callLLM: async () => (n++ === 0 ? "no json" : GOOD), maxRepairs: 2 });
    assert.equal(r.attempts, 2);
    assert.ok(r.plan);
  });
  it("extractJson handles fences", () => assert.equal(extractJson("```json\n{\"a\":1}\n```").a, 1));
});

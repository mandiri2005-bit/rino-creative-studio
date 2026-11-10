/**
 * Whiteboard delivery gate — backend/video/whiteboard/qa.mjs.
 * Run: node --test tests/node/whiteboard_qa.test.mjs
 * The parser/verdict tests are pure. The probe tests generate tiny clips with ffmpeg and are
 * skipped when ffmpeg is not installed.
 */
import { describe, it, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  parseBlackdetect, parseSilencedetect, assessClip, runDeliveryGate,
} from "../../backend/video/whiteboard/qa.mjs";

const okClip = (over = {}) => ({ ok: true, errors: [], warnings: [], duration: 10, streams: ["video", "audio"], ...over });

describe("parseBlackdetect", () => {
  it("sums every black interval", () => {
    const err = "[blackdetect @ 0x1] black_start:0 black_end:2.5 black_duration:2.5\n" +
                "[blackdetect @ 0x1] black_start:6 black_end:7 black_duration:1\n";
    assert.equal(parseBlackdetect(err), 3.5);
  });
  it("returns 0 for no matches or empty input", () => {
    assert.equal(parseBlackdetect(""), 0);
    assert.equal(parseBlackdetect(null), 0);
  });
});

describe("parseSilencedetect", () => {
  it("pairs start/end lines", () => {
    const err = "silence_start: 1\nsilence_end: 3 | silence_duration: 2\nsilence_start: 5\nsilence_end: 6\n";
    assert.equal(parseSilencedetect(err, 10), 3);
  });
  it("closes an open silence at the clip end", () => {
    assert.equal(parseSilencedetect("silence_start: 4\n", 10), 6);
  });
  it("clamps a negative start to zero", () => {
    assert.equal(parseSilencedetect("silence_start: -0.02\nsilence_end: 2\n", 10), 2);
  });
});

describe("assessClip verdicts", () => {
  it("passes a clean clip", () => {
    const r = assessClip({ clip: okClip(), content: { blackSec: 0, blankSec: 0, silenceSec: 0.5 }, expectedDuration: 10 });
    assert.equal(r.verdict, "pass");
  });
  it("rejects structural errors from the ffprobe check", () => {
    const r = assessClip({ clip: okClip({ ok: false, errors: ["clip has no video stream"] }) });
    assert.equal(r.verdict, "reject");
  });
  it("rejects a truncated clip", () => {
    const r = assessClip({ clip: okClip({ duration: 4 }), content: {}, expectedDuration: 10 });
    assert.equal(r.verdict, "reject");
    assert.match(r.reasons.join(), /truncated/);
  });
  it("rejects a black (failed) render", () => {
    assert.equal(assessClip({ clip: okClip(), content: { blackSec: 9.5 } }).verdict, "reject");
  });
  it("only warns on an empty-looking white board (sparse lineart is legitimate)", () => {
    assert.equal(assessClip({ clip: okClip(), content: { blankSec: 9.5 } }).verdict, "warn");
  });
  it("warns, not rejects, on partly black video", () => {
    assert.equal(assessClip({ clip: okClip(), content: { blackSec: 4 } }).verdict, "warn");
  });
  it("rejects missing narration", () => {
    assert.equal(assessClip({ clip: okClip({ streams: ["video"] }), content: {} }).verdict, "reject");
    assert.equal(assessClip({ clip: okClip(), content: { silenceSec: 9.8 } }).verdict, "reject");
  });
  it("ignores audio when narration is not expected", () => {
    assert.equal(assessClip({ clip: okClip({ streams: ["video"] }), content: {}, expectAudio: false }).verdict, "pass");
  });
  it("treats an unmeasured signal as no finding", () => {
    assert.equal(assessClip({ clip: okClip(), content: { blackSec: null, blankSec: null, silenceSec: null } }).verdict, "pass");
  });
});

const hasFfmpeg = spawnSync("ffmpeg", ["-version"]).status === 0 && spawnSync("ffprobe", ["-version"]).status === 0;

describe("runDeliveryGate on real clips", { skip: !hasFfmpeg && "ffmpeg not installed" }, () => {
  let dir;
  const make = (name, video, audio) => {
    const out = join(dir, name);
    const r = spawnSync("ffmpeg", ["-y", "-hide_banner", "-loglevel", "error",
      "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
      "-t", "4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out]);
    assert.equal(r.status, 0, String(r.stderr));
    return out;
  };
  before(() => { dir = mkdtempSync(join(tmpdir(), "wbqa-")); });
  after(() => { rmSync(dir, { recursive: true, force: true }); });

  it("passes a drawn board with narration", async () => {
    const f = make("ok.mp4", "testsrc2=size=320x180:rate=15", "sine=frequency=440:sample_rate=22050");
    const r = await runDeliveryGate(f, { expectedDuration: 4 });
    assert.equal(r.verdict, "pass", JSON.stringify(r));
  });
  it("rejects a black render", async () => {
    const f = make("black.mp4", "color=c=black:size=320x180:rate=15", "sine=frequency=440:sample_rate=22050");
    const r = await runDeliveryGate(f, { expectedDuration: 4 });
    assert.equal(r.verdict, "reject", JSON.stringify(r));
    assert.match(r.reasons.join(), /black/);
  });
  it("only warns on an empty white board", async () => {
    const f = make("white.mp4", "color=c=white:size=320x180:rate=15", "sine=frequency=440:sample_rate=22050");
    const r = await runDeliveryGate(f, { expectedDuration: 4 });
    assert.equal(r.verdict, "warn", JSON.stringify(r));
  });
  it("rejects silent narration", async () => {
    const f = make("mute.mp4", "testsrc2=size=320x180:rate=15", "anullsrc=r=22050:cl=mono");
    const r = await runDeliveryGate(f, { expectedDuration: 4 });
    assert.equal(r.verdict, "reject", JSON.stringify(r));
    assert.match(r.reasons.join(), /narration/);
  });
  it("observe mode reports but does not enforce", async () => {
    const prev = process.env.WB_QA_MODE;
    process.env.WB_QA_MODE = "observe";
    try {
      const f = make("black2.mp4", "color=c=black:size=320x180:rate=15", "sine=frequency=440:sample_rate=22050");
      const r = await runDeliveryGate(f, { expectedDuration: 4 });
      assert.equal(r.verdict, "reject");
      assert.equal(r.enforce, false);
    } finally {
      if (prev === undefined) delete process.env.WB_QA_MODE; else process.env.WB_QA_MODE = prev;
    }
  });
});

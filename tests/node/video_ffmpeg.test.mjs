/**
 * Node.js unit tests — backend/video/ffmpeg.mjs (the stitcher's pure builders)
 * and the worker-process guard that keeps ffmpeg off the API event loop.
 * Run: node --test tests/node/video_ffmpeg.test.mjs
 *
 * These do NOT shell out to ffmpeg (a separate integration check stitches real
 * synthetic assets). They lock the filter-graph math, the argv, and the guard.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  xfadeOffsets, buildFilterComplex, buildStitchArgs, kenBurnsExpr, runFfmpeg,
  captionWindows, buildSrt,
} from "../../backend/video/ffmpeg.mjs";
import {
  assertWorkerProcess, markVideoWorker, isVideoWorker,
} from "../../backend/video/runtime.mjs";

describe("xfadeOffsets", () => {
  it("computes running offsets and total for a 3-scene video", () => {
    const { offsets, total } = xfadeOffsets([3, 4, 2.5], 0.5);
    assert.deepEqual(offsets, [2.5, 6]);
    assert.equal(total, 8.5); // 3 + 4 + 2.5 − 2×0.5
  });
  it("single scene → no offsets, total = its duration", () => {
    assert.deepEqual(xfadeOffsets([7], 0.5), { offsets: [], total: 7 });
  });
  it("uniform scenes collapse by xfade per join", () => {
    const { total } = xfadeOffsets([5, 5, 5, 5], 0.5);
    assert.equal(total, 18.5); // 20 − 3×0.5
  });
});

describe("buildFilterComplex", () => {
  const scenes = [
    { kind: "image", duration: 3 },
    { kind: "clip", duration: 4 },
    { kind: "image", duration: 2.5 },
  ];
  it("labels every scene's v/a and exposes final vout/aout", () => {
    const { filter, vlabel, alabel, total } = buildFilterComplex(scenes, { width: 1280, height: 720, fps: 30 });
    assert.equal(vlabel, "vout");
    assert.equal(alabel, "aout");
    assert.equal(total, 8.5);
    for (let k = 0; k < 3; k++) {
      assert.match(filter, new RegExp(`\\[v${k}\\]`));
      assert.match(filter, new RegExp(`\\[a${k}\\]`));
    }
  });
  it("emits exactly n−1 xfade and n−1 acrossfade joins", () => {
    const { filter } = buildFilterComplex(scenes);
    assert.equal((filter.match(/xfade=/g) || []).length, 2);
    assert.equal((filter.match(/acrossfade=/g) || []).length, 2);
  });
  it("image scenes get zoompan (Ken Burns), clips get tpad (freeze-pad)", () => {
    const { filter } = buildFilterComplex(scenes);
    assert.match(filter, /zoompan=/);
    assert.match(filter, /tpad=stop_mode=clone/);
  });
  it("single scene → no crossfade, labels v0/a0", () => {
    const r = buildFilterComplex([{ kind: "image", duration: 5 }]);
    assert.equal(r.vlabel, "v0");
    assert.equal(r.alabel, "a0");
    assert.doesNotMatch(r.filter, /xfade=/);
  });
  it("throws on zero scenes", () => {
    assert.throws(() => buildFilterComplex([]), /no scenes/);
  });
  it("never emits a negative xfade offset, even for sub-crossfade scenes", () => {
    // scenes shorter than the 0.5s default crossfade must not produce offset<0
    const { filter } = buildFilterComplex(
      [{ kind: "image", duration: 0.3 }, { kind: "image", duration: 0.3 }],
      { width: 320, height: 240, fps: 24 }
    );
    assert.doesNotMatch(filter, /offset=-/);
  });
});

describe("buildStitchArgs", () => {
  const scenes = [
    { kind: "image", duration: 3, visualPath: "/t/img0.png", audioPath: "/t/a0.wav" },
    { kind: "clip", duration: 4, visualPath: "/t/clip1.mp4", audioPath: "/t/a1.wav" },
  ];
  const args = buildStitchArgs(scenes, "/t/out.mp4");
  it("loops image inputs but not clip inputs", () => {
    const s = args.join(" ");
    assert.match(s, /-loop 1 -t 3 -i \/t\/img0\.png/);
    assert.doesNotMatch(s, /-loop 1 -t 4 -i \/t\/clip1\.mp4/);
    assert.match(s, /-i \/t\/clip1\.mp4/);
  });
  it("has one visual + one audio input per scene (2n inputs)", () => {
    assert.equal(args.filter((a) => a === "-i").length, 4);
  });
  it("maps the final labels and encodes a faststart H.264/AAC mp4", () => {
    const s = args.join(" ");
    assert.match(s, /-map \[vout\]/);
    assert.match(s, /-map \[aout\]/);
    assert.match(s, /-c:v libx264/);
    assert.match(s, /-c:a aac/);
    assert.match(s, /\+faststart/);
    assert.equal(args[args.length - 1], "/t/out.mp4");
  });
});

describe("kenBurnsExpr", () => {
  it("produces a sized, frame-bounded zoompan", () => {
    const e = kenBurnsExpr("zoom_in", 90, 1920, 1080, 30);
    assert.match(e, /^zoompan=/);
    assert.match(e, /s=1920x1080/);
    assert.match(e, /d=90/);
  });
});

describe("captions (Step 6e) — built from the known script + measured timing", () => {
  it("captionWindows are non-overlapping and end at the master-clock total", () => {
    const w = captionWindows([3, 4, 2.5], 0.5);
    assert.deepEqual(w, [
      { index: 0, start: 0, end: 2.5 },
      { index: 1, start: 2.5, end: 6 },
      { index: 2, start: 6, end: 8.5 },
    ]);
  });
  it("buildSrt emits well-formed cues with the scene text", () => {
    const srt = buildSrt(["Hello there", "Second beat", "The end"], [3, 4, 2.5], 0.5);
    assert.match(srt, /1\n00:00:00,000 --> 00:00:02,500\nHello there/);
    assert.match(srt, /00:00:06,000 --> 00:00:08,500\nThe end/);
  });
  it("buildFilterComplex adds a subtitles pass and remaps the video label", () => {
    const r = buildFilterComplex(
      [{ kind: "image", duration: 3 }, { kind: "image", duration: 3 }],
      { srt: "captions.srt" }
    );
    assert.equal(r.vlabel, "vsub");
    assert.match(r.filter, /subtitles=captions\.srt/);
  });
  it("no subtitles pass when captions are off", () => {
    const r = buildFilterComplex([{ kind: "image", duration: 3 }, { kind: "image", duration: 3 }]);
    assert.equal(r.vlabel, "vout");
    assert.doesNotMatch(r.filter, /subtitles=/);
  });
});

describe("worker-process guard (the Step 3 discipline, in code)", () => {
  it("assertWorkerProcess throws outside the worker process", () => {
    assert.equal(isVideoWorker(), false);
    assert.throws(() => assertWorkerProcess("ffmpeg encode"), /worker process/);
  });
  it("runFfmpeg refuses to run on the API process (synchronous guard)", () => {
    // Not marked as worker → throws BEFORE spawning anything.
    assert.throws(() => runFfmpeg(["-version"]), /worker process/);
  });
  it("markVideoWorker() lifts the guard for the worker process", () => {
    markVideoWorker();
    assert.equal(isVideoWorker(), true);
    assert.doesNotThrow(() => assertWorkerProcess("ffmpeg encode"));
  });
});

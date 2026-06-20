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
  xfadeOffsets, buildFilterComplex, buildStitchArgs, buildSceneClipArgs, buildConcatArgs,
  buildXfadeConcatArgs, kenBurnsExpr, runFfmpeg, captionWindows, buildSrt, buildAss, buildAssFromScenes,
  planXfadeChunks,
} from "../../backend/video/ffmpeg.mjs";
import { CLIP_MODEL_IDS, withClipSlot, _setClipSlotsForTest } from "../../backend/video/generationClient.mjs";
import {
  assertWorkerProcess, markVideoWorker, isVideoWorker,
} from "../../backend/video/runtime.mjs";
import {
  withEncoderSlot, _setEncoderSlotsForTest, _encoderSlotStats,
} from "../../backend/video/ffmpeg-cpu.mjs";

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
    const { filter, vlabel, alabel, total } = buildFilterComplex(scenes, { width: 1280, height: 720, fps: 30, fadeDuration: 0 });
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
    const r = buildFilterComplex([{ kind: "image", duration: 5 }], { fadeDuration: 0 });
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
  const args = buildStitchArgs(scenes, "/t/out.mp4", { fadeDuration: 0 });
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
  it("buildFilterComplex adds an ass caption pass and remaps the video label", () => {
    const r = buildFilterComplex(
      [{ kind: "image", duration: 3 }, { kind: "image", duration: 3 }],
      { ass: "captions.ass", fadeDuration: 0 }
    );
    assert.equal(r.vlabel, "vsub");
    assert.match(r.filter, /ass=captions\.ass/);
    assert.doesNotMatch(r.filter, /subtitles=/);   // no more srt+force_style path
  });
  it("no caption pass when captions are off", () => {
    const r = buildFilterComplex([{ kind: "image", duration: 3 }, { kind: "image", duration: 3 }], { fadeDuration: 0 });
    assert.equal(r.vlabel, "vout");
    assert.doesNotMatch(r.filter, /ass=/);
    assert.doesNotMatch(r.filter, /subtitles=/);
  });
  it("fade: in from black at start + out to black at end, remaps v/a labels", () => {
    const r = buildFilterComplex([{ kind: "image", duration: 3 }, { kind: "image", duration: 3 }], { fadeDuration: 0.8 });
    assert.equal(r.vlabel, "vfade");
    assert.equal(r.alabel, "afade");
    assert.match(r.filter, /fade=t=in:st=0/);
    assert.match(r.filter, /fade=t=out:st=/);
    assert.match(r.filter, /afade=t=out:st=/);
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

// ── CPU oversubscription fix: per-encode thread caps + concat-copy stays clean ──
describe("encode CPU caps in the argv", () => {
  const encScenes = [
    { kind: "image", duration: 3, visualPath: "/t/i.png", audioPath: "/t/a.wav" },
    { kind: "clip", duration: 4, visualPath: "/t/c.mp4", audioPath: "/t/b.wav" },
  ];
  // indexOf on the array (not a joined string) so "-threads" can't be confused with
  // the distinct element "-filter_complex_threads".
  const after = (a, flag) => { const i = a.indexOf(flag); return i > -1 ? a[i + 1] : undefined; };

  it("buildStitchArgs caps codec + filtergraph threads by default (2/2)", () => {
    const a = buildStitchArgs(encScenes, "/t/out.mp4", { fadeDuration: 0 });
    assert.equal(after(a, "-threads"), "2");
    assert.equal(after(a, "-filter_complex_threads"), "2");
  });
  it("buildSceneClipArgs caps codec + filtergraph threads by default (2/2)", () => {
    const a = buildSceneClipArgs(encScenes[0], 0, "/t/scene_0.mp4", { fadeDuration: 0 });
    assert.equal(after(a, "-threads"), "2");
    assert.equal(after(a, "-filter_complex_threads"), "2");
  });
  it("uses the veryfast preset by default", () => {
    assert.equal(after(buildStitchArgs(encScenes, "/t/out.mp4", { fadeDuration: 0 }), "-preset"), "veryfast");
  });
  it("omits both thread flags when set to 0 (env escape hatch → ffmpeg default)", () => {
    const opts = { fadeDuration: 0, threads: 0, filterThreads: 0 };
    const a = buildStitchArgs(encScenes, "/t/out.mp4", opts);
    const b = buildSceneClipArgs(encScenes[0], 0, "/t/scene_0.mp4", opts);
    assert.equal(a.indexOf("-threads"), -1);
    assert.equal(a.indexOf("-filter_complex_threads"), -1);
    assert.equal(b.indexOf("-threads"), -1);
    assert.equal(b.indexOf("-filter_complex_threads"), -1);
  });
});

describe("buildConcatArgs (byte-join, must NOT be thread-capped)", () => {
  const c = buildConcatArgs("concat.txt", "/t/out.mp4");
  it("is a -c copy demux join with no re-encode", () => {
    const i = c.indexOf("-c");
    assert.ok(i > -1 && c[i + 1] === "copy");
    assert.ok(c.includes("concat.txt"));
    assert.equal(c[c.length - 1], "/t/out.mp4");
  });
  it("carries NEITHER -threads NOR -filter_complex_threads", () => {
    assert.equal(c.indexOf("-threads"), -1);
    assert.equal(c.indexOf("-filter_complex_threads"), -1);
  });
});

describe("withEncoderSlot (box-level encoder semaphore)", () => {
  const runN = async (n) => {
    let active = 0, peak = 0;
    const task = () => withEncoderSlot(async () => {
      active++; peak = Math.max(peak, active);
      await new Promise((r) => setTimeout(r, 20));
      active--;
    });
    await Promise.all(Array.from({ length: n }, task));
    return peak;
  };

  it("never runs more encodes than the slot count", async () => {
    _setEncoderSlotsForTest(2);
    assert.equal(await runN(6), 2);
  });
  it("is unlimited when slots <= 0 (escape hatch)", async () => {
    _setEncoderSlotsForTest(0);
    assert.equal(await runN(5), 5);
  });
  it("releases the slot even when the encode throws (no leak)", async () => {
    _setEncoderSlotsForTest(1);
    await assert.rejects(withEncoderSlot(async () => { throw new Error("boom"); }));
    let ran = false;
    await withEncoderSlot(async () => { ran = true; });
    assert.equal(ran, true);
    assert.equal(_encoderSlotStats().active, 0);
  });
});

// ── Caption styling: full .ass (replaces subtitles=srt:force_style) ──
describe("buildAss / buildAssFromScenes (styled caption burn-in)", () => {
  const ass = buildAss(
    [{ start: 0, end: 3.5, text: "Hello, world — wow" }, { start: 3.5, end: 6, text: "Second {beat}" }],
    { width: 1920, height: 1080 }
  );

  it("emits a v4.00+ script with PlayRes matched to the frame aspect", () => {
    assert.match(ass, /\[Script Info\]/);
    assert.match(ass, /ScriptType: v4\.00\+/);
    assert.match(ass, /ScaledBorderAndShadow: yes/);
    assert.match(ass, /WrapStyle: 0/);
    // 16:9 frame → PlayResX/Y in the same ratio (360 * 1920/1080 = 640)
    assert.match(ass, /PlayResX: 640/);
    assert.match(ass, /PlayResY: 360/);
  });
  it("has a [V4+ Styles] block styled per spec (font/size/bold/outline/shadow/align/margin)", () => {
    assert.match(ass, /\[V4\+ Styles\]/);
    // Default style: Liberation Sans, 22, white primary, black outline, bold=1, BorderStyle=1, Outline 2, Shadow 1, Alignment 2, MarginV 45
    assert.match(ass, /Style: Default,Liberation Sans,22,&H00FFFFFF,[^\n]*&H00000000,[^\n]*,1,0,0,0,100,100,0,0,1,2,1,2,40,40,45,1/);
  });
  it("defaults the font to Liberation Sans (Calibri/Carlito not in the worker image)", () => {
    assert.match(ass, /Style: Default,Liberation Sans,/);
  });
  it("honours an explicit captionFont override", () => {
    const a = buildAss([{ start: 0, end: 1, text: "x" }], { width: 1920, height: 1080, captionFont: "DejaVu Sans" });
    assert.match(a, /Style: Default,DejaVu Sans,/);
  });
  it("keeps commas in the narration text (Text is the field after the 9th comma)", () => {
    // This is the whole point of moving off subtitles=srt:force_style.
    assert.match(ass, /Dialogue: 0,0:00:00\.00,0:00:03\.50,Default,,0,0,0,,Hello, world — wow/);
  });
  it("neutralises ASS override braces and uses centisecond timestamps", () => {
    assert.match(ass, /,Second \(beat\)/);          // {} → ()
    assert.doesNotMatch(ass, /\{beat\}/);
    assert.match(ass, /0:00:03\.50,0:00:06\.00/);   // H:MM:SS.cc
  });
  it("buildAssFromScenes lays cues on the caption-window timeline", () => {
    const a = buildAssFromScenes(["one", "two", "three"], [3, 4, 2.5], 0.5, { width: 1920, height: 1080 });
    // windows: [0,2.5],[2.5,6],[6,8.5]
    assert.match(a, /Dialogue: 0,0:00:00\.00,0:00:02\.50,Default,,0,0,0,,one/);
    assert.match(a, /Dialogue: 0,0:00:06\.00,0:00:08\.50,Default,,0,0,0,,three/);
  });
  it("skips empty/blank scene text (no stray Dialogue line)", () => {
    const a = buildAss([{ start: 0, end: 1, text: "  " }, { start: 1, end: 2, text: "real" }], { width: 1920, height: 1080 });
    assert.equal((a.match(/^Dialogue:/gm) || []).length, 1);
    assert.match(a, /,real/);
  });
});

// ── Veo model IDs must match laozhang official-forward (only *-generate-preview) ──
describe("CLIP_MODEL_IDS — valid laozhang Veo model names", () => {
  it("maps every veo alias to a *-generate-preview name (no legacy → no 503 no-channels)", () => {
    for (const k of ["veo3", "veo3_fast", "veo3_pro"]) {
      assert.match(CLIP_MODEL_IDS[k], /-generate-preview$/, `${k} must be a *-generate-preview model`);
    }
    assert.equal(CLIP_MODEL_IDS.veo3_fast, "veo-3.1-fast-generate-preview");
    assert.equal(CLIP_MODEL_IDS.veo3, "veo-3.1-generate-preview");
  });
  it("contains NONE of the laozhang-forbidden legacy names", () => {
    const vals = Object.values(CLIP_MODEL_IDS);
    for (const bad of ["veo-3.1", "veo-3.1-fast", "veo-3.1-fl"]) {
      assert.ok(!vals.includes(bad), `legacy "${bad}" must not be used`);
    }
  });
});

// ── Clip-submit de-burst semaphore (Veo 429 "upstream load saturated" mitigation) ──
describe("withClipSlot — caps concurrent Veo submits to de-burst the upstream", () => {
  it("never lets more than N clip submits run at once", async () => {
    _setClipSlotsForTest(2);
    let live = 0, peak = 0;
    const task = () => withClipSlot(async () => {
      live++; peak = Math.max(peak, live);
      await new Promise((r) => setTimeout(r, 15));
      live--;
    });
    await Promise.all(Array.from({ length: 8 }, task));
    assert.equal(peak, 2, `peak concurrency should be capped at 2, got ${peak}`);
    assert.equal(live, 0, "all slots released");
    _setClipSlotsForTest(3); // reset to the default cap (min clamp is 1)
  });
  it("releases the slot even when the task throws", async () => {
    _setClipSlotsForTest(1);
    await assert.rejects(withClipSlot(async () => { throw new Error("boom"); }), /boom/);
    // if the slot leaked, this second call would hang forever (await would never resolve)
    let ran = false;
    await withClipSlot(async () => { ran = true; });
    assert.ok(ran, "slot was released after the throw");
    _setClipSlotsForTest(3);
  });
});

// ── Bounded-memory chunk planner (crossfade at any scene count without OOM) ──
describe("planXfadeChunks (bounded-memory chunking)", () => {
  it("returns the whole list as one chunk when it fits", () => {
    assert.deepEqual(planXfadeChunks(8, 12), [[0, 8]]);
    assert.deepEqual(planXfadeChunks(12, 12), [[0, 12]]);
  });
  it("splits into chunks of at most chunkSize (15 @ 6 → 6+6+3)", () => {
    assert.deepEqual(planXfadeChunks(15, 6), [[0, 6], [6, 6], [12, 3]]);
  });
  it("keeps a lone tail chunk of 1 (13 @ 6 → 6+6+1)", () => {
    assert.deepEqual(planXfadeChunks(13, 6), [[0, 6], [6, 6], [12, 1]]);
  });
  it("clamps a degenerate chunkSize to >=2 (never an infinite loop)", () => {
    assert.deepEqual(planXfadeChunks(5, 1), [[0, 2], [2, 2], [4, 1]]);
    assert.deepEqual(planXfadeChunks(5, 0), [[0, 2], [2, 2], [4, 1]]);
  });
  it("covers every clip exactly once (no gaps/overlap) for many sizes", () => {
    for (const [n, k] of [[15, 12], [25, 12], [100, 12], [13, 6], [7, 3]]) {
      const chunks = planXfadeChunks(n, k);
      const covered = chunks.flatMap(([s, len]) => Array.from({ length: len }, (_, i) => s + i));
      assert.deepEqual(covered, Array.from({ length: n }, (_, i) => i), `n=${n} k=${k}`);
      assert.ok(chunks.every(([, len]) => len <= k), `no chunk exceeds k for n=${n} k=${k}`);
    }
  });
});

// ── Seam crossfade for the per-scene (long-video) path ──
describe("buildXfadeConcatArgs (seam crossfade re-encode)", () => {
  const clips = ["/t/scene_0.mp4", "/t/scene_1.mp4", "/t/scene_2.mp4"];
  const a = buildXfadeConcatArgs(clips, [5, 4, 6], "/t/out.mp4", { fps: 30 });
  const s = a.join(" ");
  it("loads every clip as an -i input", () => {
    assert.equal(a.filter((x) => x === "-i").length, 3);
  });
  it("builds N-1 xfade + N-1 acrossfade, ending in vout/aout", () => {
    assert.equal((s.match(/xfade=transition=/g) || []).length, 2);
    assert.equal((s.match(/acrossfade=d=/g) || []).length, 2);
    assert.match(s, /-map \[vout\] -map \[aout\]/);
  });
  it("re-encodes h264 (NOT -c copy) with thread caps + veryfast", () => {
    assert.match(s, /-c:v libx264/);
    assert.doesNotMatch(s, /-c copy/);
    assert.match(s, /-threads 2/);
    assert.match(s, /-preset veryfast/);
  });
  it("uses running xfade offsets (XF=0.4 → first offset = dur0 - XF = 4.6)", () => {
    assert.match(s, /offset=4\.6/);
  });
});

/**
 * Node.js unit tests — backend/utils.mjs
 * Run: node --test tests/node/helpers.test.mjs
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  parseAudioMime, makeWavHeader, convertToWav, prependSilence,
  buildJsonl, badgeFor, calcCost, estTok, parseSSELine,
} from "../../backend/utils.mjs";

// ─── parseAudioMime ───────────────────────────────────────────────────────────
describe("parseAudioMime", () => {
  it("parses L16/24000", () => {
    assert.deepEqual(parseAudioMime("audio/L16;rate=24000"), { bits: 16, rate: 24000 });
  });
  it("parses L24/48000", () => {
    assert.deepEqual(parseAudioMime("audio/L24;rate=48000"), { bits: 24, rate: 48000 });
  });
  it("defaults bits=16 rate=24000 for unknown mime", () => {
    assert.deepEqual(parseAudioMime("audio/unknown"), { bits: 16, rate: 24000 });
  });
  it("is case-insensitive on rate=", () => {
    assert.equal(parseAudioMime("audio/L16;Rate=44100").rate, 44100);
  });
});

// ─── makeWavHeader ────────────────────────────────────────────────────────────
describe("makeWavHeader", () => {
  it("produces exactly 44 bytes", () => {
    assert.equal(makeWavHeader(1000).length, 44);
  });
  it("writes RIFF/WAVE/fmt/data markers", () => {
    const h = makeWavHeader(0);
    assert.equal(h.toString("ascii", 0, 4),  "RIFF");
    assert.equal(h.toString("ascii", 8, 12), "WAVE");
    assert.equal(h.toString("ascii", 12, 16),"fmt ");
    assert.equal(h.toString("ascii", 36, 40),"data");
  });
  it("RIFF chunk size = 36 + dataLen", () => {
    assert.equal(makeWavHeader(500).readUInt32LE(4), 536);
  });
  it("data chunk size = dataLen", () => {
    assert.equal(makeWavHeader(1234).readUInt32LE(40), 1234);
  });
  it("writes sample rate", () => {
    assert.equal(makeWavHeader(0, 16, 48000, 1).readUInt32LE(24), 48000);
  });
  it("writes bit depth and channels", () => {
    const h = makeWavHeader(0, 24, 44100, 2);
    assert.equal(h.readUInt16LE(34), 24);   // bits per sample
    assert.equal(h.readUInt16LE(22), 2);    // channels
  });
});

// ─── convertToWav ────────────────────────────────────────────────────────────
describe("convertToWav", () => {
  it("output = 44 + raw.length", () => {
    const raw = Buffer.alloc(200);
    assert.equal(convertToWav(raw, "audio/L16;rate=24000").length, 244);
  });
  it("starts with RIFF", () => {
    const wav = convertToWav(Buffer.alloc(4), "audio/L16;rate=24000");
    assert.equal(wav.toString("ascii", 0, 4), "RIFF");
  });
  it("audio payload is preserved after header", () => {
    const raw = Buffer.from([0xAB, 0xCD, 0xEF]);
    const wav = convertToWav(raw, "audio/L16;rate=24000");
    assert.deepEqual(wav.slice(44), raw);
  });
});

// ─── prependSilence ──────────────────────────────────────────────────────────
describe("prependSilence", () => {
  it("output is longer than input", () => {
    const wav = convertToWav(Buffer.alloc(100), "audio/L16;rate=24000");
    assert.ok(prependSilence(wav, 0.5).length > wav.length);
  });
  it("result is still valid WAV (starts RIFF)", () => {
    const wav = convertToWav(Buffer.alloc(100), "audio/L16;rate=24000");
    assert.equal(prependSilence(wav, 0.1).toString("ascii", 0, 4), "RIFF");
  });
  it("1 sec silence at 24k/16bit/mono adds exactly 48000 bytes", () => {
    // 24000 samples × 2 bytes × 1 channel = 48000 bytes
    const wav = convertToWav(Buffer.alloc(0), "audio/L16;rate=24000");
    assert.equal(prependSilence(wav, 1.0).length, 44 + 48000);
  });
  it("zero silence returns same length", () => {
    const wav = convertToWav(Buffer.alloc(100), "audio/L16;rate=24000");
    assert.equal(prependSilence(wav, 0).length, wav.length);
  });
});

// ─── buildJsonl ──────────────────────────────────────────────────────────────
describe("buildJsonl", () => {
  const S = { aspectRatio: "16:9", imageSize: "1K" };

  it("one line per job", () => {
    const lines = buildJsonl(S, [{ prompt: "a" }, { prompt: "b" }]).split("\n").filter(Boolean);
    assert.equal(lines.length, 2);
  });
  it("keys are image-1, image-2, …", () => {
    const objs = buildJsonl(S, [{ prompt: "a" }, { prompt: "b" }, { prompt: "c" }])
      .split("\n").map(JSON.parse);
    assert.deepEqual(objs.map(o => o.key), ["image-1", "image-2", "image-3"]);
  });
  it("prompt lands in contents.parts[0].text", () => {
    const obj = JSON.parse(buildJsonl(S, [{ prompt: "sunny beach" }]));
    assert.equal(obj.request.contents[0].parts[0].text, "sunny beach");
  });
  it("respects custom aspect + size", () => {
    const cfg = JSON.parse(buildJsonl({ aspectRatio: "9:16", imageSize: "4K" }, [{ prompt: "x" }]))
      .request.generation_config.imageConfig;
    assert.equal(cfg.aspectRatio, "9:16");
    assert.equal(cfg.imageSize,   "4K");
  });
  it("defaults when settings empty", () => {
    const cfg = JSON.parse(buildJsonl({}, [{ prompt: "x" }])).request.generation_config.imageConfig;
    assert.equal(cfg.aspectRatio, "16:9");
    assert.equal(cfg.imageSize,   "1K");
  });
  it("empty jobs → empty string", () => assert.equal(buildJsonl(S, []), ""));
});

// ─── badgeFor ────────────────────────────────────────────────────────────────
describe("badgeFor", () => {
  it("JOB_STATE_SUCCEEDED → ok/done",  () => assert.deepEqual(badgeFor("JOB_STATE_SUCCEEDED"), ["ok",  "done"]));
  it("DONE → ok/done",                 () => assert.deepEqual(badgeFor("DONE"),                ["ok",  "done"]));
  it("JOB_STATE_RUNNING → run",        () => assert.equal(badgeFor("JOB_STATE_RUNNING")[0],    "run"));
  it("QUEUED → pend",                  () => assert.equal(badgeFor("QUEUED")[0],               "pend"));
  it("JOB_STATE_FAILED → fail",        () => assert.equal(badgeFor("JOB_STATE_FAILED")[0],     "fail"));
  it("JOB_STATE_CANCELLED → fail",     () => assert.equal(badgeFor("JOB_STATE_CANCELLED")[0],  "fail"));
  it("null → pend/—",                  () => assert.deepEqual(badgeFor(null),                  ["pend","—"]));
  it("undefined → pend/—",             () => assert.deepEqual(badgeFor(undefined),             ["pend","—"]));
});

// ─── calcCost ────────────────────────────────────────────────────────────────
describe("calcCost", () => {
  it("zero tokens → zero cost", () => assert.equal(calcCost([0.30, 2.40], 0, 0), 0));
  it("Gemini 2.5 Flash: 1M/1M = $2.70", () => assert.equal(calcCost([0.30, 2.40], 1e6, 1e6), 2.70));
  it("only input tokens", ()  => assert.equal(calcCost([2.50, 10.00], 1e6, 0),  2.50));
  it("only output tokens", () => assert.equal(calcCost([2.50, 10.00], 0,   1e6), 10.00));
  it("small request cost", () => {
    // 500 in + 200 out, GPT-4o-mini $0.15/$0.60
    const cost = calcCost([0.15, 0.60], 500, 200);
    assert.equal(Math.round(cost * 1e9), Math.round((500 * 0.15 + 200 * 0.60) / 1e6 * 1e9));
  });
});

// ─── estTok ──────────────────────────────────────────────────────────────────
describe("estTok", () => {
  it("empty string → 0", () => assert.equal(estTok(""), 0));
  it("null → 0",         () => assert.equal(estTok(null), 0));
  it("undefined → 0",    () => assert.equal(estTok(undefined), 0));
  it("4 chars → 1-2 tokens", () => { const t = estTok("abcd"); assert.ok(t >= 1 && t <= 2); });
  it("380 chars → ~100 tokens", () => {
    const t = estTok("x".repeat(380));
    assert.ok(t >= 95 && t <= 105, `expected ~100, got ${t}`);
  });
  it("scales proportionally", () => {
    assert.ok(estTok("a".repeat(80)) > estTok("a".repeat(40)));
  });
});

// ─── parseSSELine ─────────────────────────────────────────────────────────────
describe("parseSSELine", () => {
  it("non-data line → null", () => {
    assert.equal(parseSSELine("event: ping"), null);
    assert.equal(parseSSELine(": keepalive"), null);
    assert.equal(parseSSELine(""), null);
  });
  it("[DONE] → {type:done}", () => assert.deepEqual(parseSSELine("data: [DONE]"), { type: "done" }));
  it("[ERROR] → {type:error}", () => {
    const r = parseSSELine("data: [ERROR: timeout]");
    assert.equal(r.type, "error");
    assert.ok(r.message.includes("timeout"));
  });
  it("[USAGE:] → {type:usage,input,output}", () => {
    assert.deepEqual(
      parseSSELine('data: [USAGE:{"input":312,"output":127}]'),
      { type: "usage", input: 312, output: 127 }
    );
  });
  it("malformed [USAGE:] → null", () => {
    assert.equal(parseSSELine("data: [USAGE:notjson]"), null);
  });
  it("[TOOL_CALL] → {type:tool}", () => {
    const r = parseSSELine('data: [TOOL_CALL]search_files({"query":"test"})');
    assert.equal(r.type, "tool");
    assert.ok(r.event.includes("search_files"));
  });
  it("[TOOL_RESULT] → {type:tool}", () => {
    assert.equal(parseSSELine("data: [TOOL_RESULT]found 3 files").type, "tool");
  });
  it("text token → {type:text,text}", () => {
    assert.deepEqual(parseSSELine("data: Hello"), { type: "text", text: "Hello" });
  });
  it("preserves leading space in text", () => {
    assert.equal(parseSSELine("data:  word").text, " word");
  });
  it("multiword text preserved", () => {
    assert.equal(parseSSELine("data: foo bar baz").text, "foo bar baz");
  });
});

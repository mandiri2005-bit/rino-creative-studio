/**
 * Node.js route inspection tests — no server startup needed.
 * Verifies routes are registered, headers forwarded, and key behaviors coded.
 * Run: node --test tests/node/routes.test.mjs
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SERVER_CODE = fs.readFileSync(
  path.join(__dirname, "../../backend/server.js"), "utf8"
);

// ─── Route registration ───────────────────────────────────────────────────────
describe("Route registration", () => {
  const REQUIRED_ROUTES = [
    ["get",  "/api/health"],
    ["get",  "/api/config"],
    ["post", "/api/chat"],
    ["post", "/api/chat/google"],
    ["post", "/api/cancel/"],
    ["get",  "/api/history/"],
    ["post", "/api/upload"],
    ["get",  "/api/image-models"],
    ["post", "/api/generate-image"],
    ["post", "/api/generate-image/google"],
    ["get",  "/api/generate-image/google/models"],
    ["post", "/api/whisk"],
    ["post", "/api/whisk/google"],
    ["post", "/api/flow/storyboard"],
    ["post", "/api/flow/storyboard/google"],
    ["post", "/api/veo/submit"],
    ["get",  "/api/veo/status/"],
    ["get",  "/api/veo/stream/"],
    ["post", "/api/sora/submit"],
    ["get",  "/api/sora/status/"],
    ["get",  "/api/sora/stream/"],
    ["get",  "/api/mcp/files"],
    ["get",  "/api/mcp/search"],
    ["post", "/api/mcp/reindex"],
    ["get",  "/api/jobs"],
    ["post", "/api/submit"],
    ["post", "/api/retrieve"],
    ["get",  "/api/images"],
    ["post", "/api/tts/start"],
    ["get",  "/api/tts/job/"],
    ["get",  "/api/tts/files"],
    ["post", "/api/imagen/start"],
    ["get",  "/api/imagen/job/"],
    ["get",  "/api/imagen/files"],
  ];

  for (const [method, route] of REQUIRED_ROUTES) {
    it(`${method.toUpperCase()} ${route} is registered`, () => {
      // Allow optional space: app.get("/api/..." or app.get ("/api/..."
      const p1 = `app.${method}("${route}`;
      const p2 = `app.${method} ("${route}`;
      assert.ok(SERVER_CODE.includes(p1) || SERVER_CODE.includes(p2),
        `Missing: ${method.toUpperCase()} ${route}`);
    });
  }
});

// ─── Header forwarding ────────────────────────────────────────────────────────
describe("Header forwarding in pyProxy", () => {
  it("forwards x-laozhang-api-key",  () => assert.ok(SERVER_CODE.includes("x-laozhang-api-key")));
  it("forwards X-LaoZhang-API-Key",  () => assert.ok(SERVER_CODE.includes("X-LaoZhang-API-Key")));
  it("forwards x-image-api-key",     () => assert.ok(SERVER_CODE.includes("x-image-api-key")));
  it("forwards x-veo-api-key",       () => assert.ok(SERVER_CODE.includes("x-veo-api-key")));
  it("forwards x-sora-api-key",      () => assert.ok(SERVER_CODE.includes("x-sora-api-key")));
});

// ─── Google chat features ─────────────────────────────────────────────────────
describe("Google chat route features", () => {
  it("reads thinkingLevel from request body", () =>
    assert.ok(SERVER_CODE.includes("thinkingLevel")));
  it("sets thinkingConfig for gemini-3 models", () =>
    assert.ok(SERVER_CODE.includes("thinkingConfig")));
  it("checks model starts with gemini-3", () =>
    assert.ok(SERVER_CODE.includes('gemini-3')));
  it("uses generateContentStream for streaming", () =>
    assert.ok(SERVER_CODE.includes("generateContentStream")));
  it("reads usageMetadata from stream chunks", () =>
    assert.ok(SERVER_CODE.includes("usageMetadata")));
  it("emits [USAGE:] SSE event with token counts", () =>
    assert.ok(SERVER_CODE.includes("[USAGE:")));
  it("tracks inputTokens and outputTokens", () => {
    assert.ok(SERVER_CODE.includes("inputTokens"));
    assert.ok(SERVER_CODE.includes("outputTokens"));
  });
  it("uses temperature 1.0 for Google chat (Gemini 3 recommendation)", () =>
    assert.ok(SERVER_CODE.includes("temperature: 1.0") ||
               SERVER_CODE.includes("temperature:1.0")));
});

// ─── Google image routes ──────────────────────────────────────────────────────
describe("Google image routes", () => {
  it("generate-image/google uses generateImages", () =>
    assert.ok(SERVER_CODE.includes("generateImages")));
  it("has GOOGLE_IMG_MODELS list", () =>
    assert.ok(SERVER_CODE.includes("GOOGLE_IMG_MODELS")));
  it("whisk/google uses generateContent", () =>
    assert.ok(SERVER_CODE.includes("generateContent")));
  it("whisk/google uses Gemini multimodal model", () =>
    assert.ok(SERVER_CODE.includes("gemini-2.0-flash-exp")));
  it("whisk/google requests IMAGE response modality", () =>
    assert.ok(SERVER_CODE.includes("responseModalities")));
  it("flow/storyboard/google uses generateImages per scene", () => {
    assert.ok(SERVER_CODE.includes("/api/flow/storyboard/google"));
    assert.ok(SERVER_CODE.includes("allSettled"));   // parallel generation
  });
});

// ─── Environment configuration ───────────────────────────────────────────────
describe("Environment configuration", () => {
  it("PYTHON_API_URL falls back to 127.0.0.1:8000", () =>
    assert.ok(SERVER_CODE.includes("http://127.0.0.1:8000")));
  it("MCP_API_URL falls back to 127.0.0.1:8001", () =>
    assert.ok(SERVER_CODE.includes("http://127.0.0.1:8001")));
  it("GEMINI_API_KEY read from env", () =>
    assert.ok(SERVER_CODE.includes("GEMINI_API_KEY")));
  it("imports from utils.mjs", () =>
    assert.ok(SERVER_CODE.includes("./utils.mjs")));
});

// ─── TTS key rotation ─────────────────────────────────────────────────────────
describe("TTS key rotation logic", () => {
  it("rotates keys on 429 or RESOURCE_EXHAUSTED", () =>
    assert.ok(SERVER_CODE.includes("RESOURCE_EXHAUSTED") ||
               SERVER_CODE.includes("429")));
  it("supports multiple API keys array", () =>
    assert.ok(SERVER_CODE.includes("apiKeys")));
  it("tracks key index for rotation", () =>
    assert.ok(SERVER_CODE.includes("keyIdx")));
  it("logs key rotation event", () =>
    assert.ok(SERVER_CODE.includes("Key #")));
});

// ─── Batch image output naming ────────────────────────────────────────────────
describe("Batch image output naming", () => {
  it("uses KEY_TO_OUTPUT mapping from job record", () =>
    assert.ok(SERVER_CODE.includes("mapping")));
  it("saves retrieved images with mapped filename", () =>
    assert.ok(SERVER_CODE.includes("mapOf")));
  it("saves PNGs to OUTPUT_DIR", () =>
    assert.ok(SERVER_CODE.includes("OUTPUT_DIR")));
});

// ─── Video streaming (Veo/Sora) ───────────────────────────────────────────────
describe("Video streaming routes", () => {
  it("Veo stream reads full MP4 into buffer before sending", () =>
    assert.ok(SERVER_CODE.includes("arrayBuffer")));
  it("Sora stream sets video/mp4 content-type", () =>
    assert.ok(SERVER_CODE.includes("video/mp4")));
  it("streams are not cached", () =>
    assert.ok(SERVER_CODE.includes("no-store")));
});

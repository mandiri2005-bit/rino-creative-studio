import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

/**
 * Guard on the SHIPPED storyboard export — `backend/public/storyboard/`, served at
 * `app.wimba.ai/storyboard`, which is the Script tool users actually run.
 *
 * 🔴 WHAT THIS FILE IS, AND WHAT IT IS NOT. These are artifact assertions: they prove the
 *    committed bytes were built from a source that carries the terminal-recovery fix, so a
 *    later rebuild from a stale checkout cannot silently ship the blank panel again. They do
 *    NOT prove the panel renders — a string can be present and the component still blank.
 *
 *    The behavioural proof is `tests/node/storyboard_terminal_ui_harness.mjs`, which serves
 *    THESE bytes with a mocked narration API and is driven in a real browser. Under the real
 *    backend status sequence (pending → running → failed) the pre-fix export renders 66
 *    characters — page chrome only, no error, no spinner: canary `7pucr0hs`'s blank panel.
 *    The fixed export renders the recovery card, keeps the job id, and offers a route back.
 *
 * 🔴 THE STATUS SEQUENCE IS LODAD-BEARING. `narration_api` emits only
 *    {pending, running, polishing, done, failed, cancelled}. NarasiTool's auto-resume effect
 *    reattaches on ["running","queued","polishing","processing"] — `pending` is not in it — so
 *    a job observed at `pending` right after start leaves the FOREGROUND poll as its only
 *    watcher. That is the lane that was missing `setJobError`. A fixture that answers
 *    "processing" tests `watchJob` instead and passes on a broken build.
 */

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");
const exportDir = path.join(root, "backend/public/storyboard");

const chunks = () => {
  const dir = path.join(exportDir, "_next/static/chunks");
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".js"))
    .map((f) => fs.readFileSync(path.join(dir, f), "utf8"));
};

const bundleText = () => chunks().join("\n");

test("the storyboard export is present and is the app it claims to be", () => {
  const index = fs.readFileSync(path.join(exportDir, "index.html"), "utf8");
  assert.match(index, /window\.__BRAND="wimba"/);
  // basePath applied — a build without STORYBOARD_BASE_PATH 404s every asset in production.
  assert.match(index, /\/storyboard\/_next/);
  assert.equal((index.match(/"\/_next\//g) || []).length, 0, "bare /_next/ means no basePath");
  // The publishable key stays a placeholder in the repo; nginx substitutes it at serve time.
  assert.match(index, /__CLERK_PK="__CLERK_PK_ENV__"/);
});

test("the shipped bundle carries the terminal recovery card", () => {
  const bundle = bundleText();
  assert.match(bundle, /data-narasi-recovery/);
  assert.match(bundle, /Narration didn't complete|Narration didn.{1,8}t complete/);
});

test("every terminal outcome has a message the recovery card can show", () => {
  const bundle = bundleText();
  // failed / error
  assert.match(bundle, /Narration failed for an unknown reason\./);
  // cancelled
  assert.match(bundle, /Narration was cancelled\./);
  // done with an empty manuscript — the shape a gate refusal produces
  assert.match(bundle, /Narration finished but returned no content\./);
});

test("the recovery card offers a destination that actually renders", () => {
  const bundle = bundleText();
  assert.match(bundle, /data-narasi-recovery-action/);
  assert.match(bundle, /Back to outline/);
  assert.match(bundle, /Back to input/);
  assert.match(bundle, /Start over/);
});

test("stage 'outline' with no outline has its own recovery card", () => {
  const bundle = bundleText();
  assert.match(bundle, /outline-missing/);
  assert.match(bundle, /Outline no longer available/);
});

test("the harness fixture uses statuses the backend can actually emit", () => {
  // A regression guard on the FIXTURE: answering "processing" makes the auto-resume effect
  // reattach and the harness silently tests the wrong lane.
  const harness = fs.readFileSync(path.join(here, "storyboard_terminal_ui_harness.mjs"), "utf8");
  assert.match(harness, /status: "pending"/);
  assert.match(harness, /status: "running"/);
  assert.doesNotMatch(
    harness,
    /status: "processing"/,
    "the backend never emits 'processing' — it would exercise watchJob, not the foreground poll",
  );
});

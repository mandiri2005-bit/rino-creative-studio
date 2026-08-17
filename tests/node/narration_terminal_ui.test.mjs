import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");

const source = fs.readFileSync(
  path.join(root, "backend/public/classic-studio.html"),
  "utf8",
);

test("classic Dalang returns to the outline when no manuscript is delivered", () => {
  assert.match(source, /if\(!finalOut\) setStage\("outline"\);/);
});

test("every empty terminal result renders a recovery card instead of a blank panel", () => {
  assert.match(source, /!generating&&!narasi&&/);
  assert.match(source, /data-narasi-terminal-fallback="true"/);
  assert.match(source, /Kembali ke Outline &amp; Coba Lagi/);
});

test("classic polling treats failed as terminal and never stitches it", () => {
  assert.match(
    source,
    /sd\.status==="done"\|\|sd\.status==="failed"\|\|sd\.status==="cancelled"\|\|sd\.status==="error"/,
  );
  assert.match(source, /if\(sd\.status!=="done"\) abortRef\.current=true;/);
  assert.match(source, /if\(abortRef\.current\)\{ setGenerating\(false\); return; \}/);
});

test("the active app bundle does not leave an empty result panel", () => {
  const index = fs.readFileSync(
    path.join(root, "backend/public/app/index.html"),
    "utf8",
  );
  const match = index.match(/src="\/app\/assets\/(main-[^"]+\.js)(?:\?[^"]*)?"/);
  assert.ok(match, "index must name the active app bundle");

  const bundle = fs.readFileSync(
    path.join(root, "backend/public/app/assets", match[1]),
    "utf8",
  );
  assert.match(bundle, /data-narasi-terminal-fallback/);
  assert.match(bundle, /Kembali ke Outline & Coba Lagi/);
  assert.match(bundle, /status===`done`\|\|\w+\.status===`failed`\|\|\w+\.status===`cancelled`\|\|\w+\.status===`error`/);
  assert.match(index, /\?v=narasi-terminal-fallback-2/);
});

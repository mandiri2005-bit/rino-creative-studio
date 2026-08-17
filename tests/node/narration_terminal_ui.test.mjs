import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../..");

test("classic Dalang returns to the outline when no manuscript is delivered", () => {
  const source = fs.readFileSync(
    path.join(root, "backend/public/classic-studio.html"),
    "utf8",
  );
  assert.match(source, /if\(!finalOut\) setStage\("outline"\);/);
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
  assert.match(bundle, /s\|\|he\(`outline`\),U\(s\);try\{Je\(d\)\}catch\{\}/);
  assert.match(index, /\?v=narasi-terminal-fallback-1/);
});

#!/usr/bin/env node
/**
 * Storyboard terminal-UI harness — serves the REAL built export with a mocked narration API.
 *
 * 🔴 WHY THIS EXISTS. The blank-panel bug (canary `7pucr0hs`) is a STATE bug, not a string bug:
 *    the foreground poll in `NarasiTool.approveAndGenerate` reached a terminal status, showed a
 *    toast, and left `stage="result" / generating=false / narasi="" / jobError=null`, which
 *    renders an empty panel. No amount of grepping the bundle for a substring proves the panel
 *    is not blank — only driving the built artifact in a browser does.
 *
 *    This server is that fixture. It serves `backend/public/storyboard/` at the SAME path
 *    production uses (`/storyboard`) and answers the narration endpoints with scripted
 *    terminal states, so a browser can walk processing → terminal and be asked what it renders.
 *
 * Usage:
 *     node tests/node/storyboard_terminal_ui_harness.mjs [port]
 *
 * Then drive http://127.0.0.1:<port>/storyboard/ in a browser. Switch the scripted outcome at
 * any time with `GET /__scenario/<name>`:
 *
 *     failed       — status "failed" with an error and no output   (the canary shape)
 *     error        — status "error"  with an error and no output
 *     cancelled    — status "cancelled"
 *     done-empty   — status "done" with an EMPTY manuscript        (a gate refusal)
 *     done         — status "done" with a manuscript               (the happy path)
 *
 * Every scenario reports "processing" for the first two polls first, so the run is observed
 * going through the generating state rather than starting terminal.
 *
 * The app runs Clerk-less here (`__CLERK_PK_ENV__` is not a valid key ⇒ `authed === false` ⇒
 * the project gate soft-bypasses), which is what makes an unauthenticated drive possible.
 */
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(here, "../..");
const EXPORT_DIR = path.join(ROOT, "backend/public/storyboard");
const PORT = Number(process.argv[2] || 4173);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

const TERMINALS = {
  failed: { status: "failed", error: "f6_unresolved_hard_violation", output: "" },
  error: { status: "error", error: "narration worker crashed", output: "" },
  cancelled: { status: "cancelled", output: "" },
  "done-empty": { status: "done", output: "" },
  done: { status: "done", output: "## Chapter 1: Kembali\n\nEun-soo membuka pintu kantor lama itu.\n" },
};

let scenario = "failed";
let polls = 0;

const json = (res, body, code = 200) => {
  res.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  res.end(JSON.stringify(body));
};

const readBody = (req) =>
  new Promise((resolve) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      try {
        resolve(JSON.parse(raw || "{}"));
      } catch {
        resolve({});
      }
    });
  });

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const pathname = decodeURIComponent(url.pathname);

  // ── control plane ────────────────────────────────────────────────────────
  if (pathname.startsWith("/__scenario/")) {
    const name = pathname.slice("/__scenario/".length);
    if (!TERMINALS[name]) return json(res, { ok: false, known: Object.keys(TERMINALS) }, 400);
    scenario = name;
    polls = 0;
    return json(res, { ok: true, scenario });
  }
  if (pathname === "/__reset") {
    polls = 0;
    return json(res, { ok: true, scenario });
  }

  // ── mocked narration API ─────────────────────────────────────────────────
  if (pathname === "/api/narration" && req.method === "POST") {
    await readBody(req);
    polls = 0;
    return json(res, { job_id: `mock-${scenario}` }, 202);
  }
  if (/^\/api\/narration\/[^/]+\/cancel$/.test(pathname)) return json(res, { ok: true });
  if (/^\/api\/narration\/[^/]+$/.test(pathname)) {
    polls += 1;
    // 🔴 THE FIRST READ MUST BE "pending", AND THAT IS THE WHOLE FIXTURE. `narration_api`
    //    publishes exactly {pending, running, polishing, done, failed, cancelled}. The
    //    auto-resume effect in NarasiTool reattaches only on
    //    ["running","queued","polishing","processing"] — `pending` is NOT in it. So when the
    //    foreground run sets `savedJobId`, auto-resume polls once, sees a job that has only
    //    just been enqueued, matches nothing, and marks itself done forever: `watchJob` never
    //    runs, and the FOREGROUND poll is the only watcher of that job. That is precisely how
    //    production reaches the blank panel.
    //
    //    An earlier version of this harness answered "processing" — a status the backend never
    //    emits — which DID match the auto-resume list, started `watchJob`, and let watchJob's
    //    (already-correct) terminal handling paint the recovery card. The fixture passed while
    //    testing the wrong lane. Do not reintroduce a status the server cannot produce.
    if (polls === 1) return json(res, { status: "pending", done: 0 });
    if (polls === 2) {
      return json(res, {
        status: "running",
        progress: "Writing chapter 1 of 3 …",
        chapters: [{ state: "done" }, { state: "pending" }, { state: "pending" }],
        done: 1,
      });
    }
    return json(res, { found: true, ...TERMINALS[scenario] });
  }
  if (pathname === "/api/narasi/outline" && req.method === "POST") {
    return json(res, {
      chapters: [
        { id: 1, title: "Kembali", description: "Eun-soo returns to the office", words: 400 },
        { id: 2, title: "Lobi", description: "Min-jae waits for the witness list", words: 400 },
      ],
      outline_text: "1. Kembali — Eun-soo returns\n2. Lobi — Min-jae waits\n",
    });
  }
  if (pathname === "/api/narasi/jobs") return json(res, { ok: true, jobs: [] });
  if (pathname.startsWith("/api/narasi/chapters")) return json(res, { ok: true, chapters: [] });
  if (pathname === "/api/projects") return json(res, { projects: [] });
  if (pathname.startsWith("/api/")) return json(res, { ok: false, error: "not mocked" }, 404);

  // ── static export, served at the production path ─────────────────────────
  if (pathname === "/" || pathname === "/storyboard") {
    res.writeHead(302, { location: "/storyboard/" });
    return res.end();
  }
  if (!pathname.startsWith("/storyboard/")) {
    res.writeHead(404);
    return res.end("not found");
  }
  let rel = pathname.slice("/storyboard/".length) || "index.html";
  if (rel.endsWith("/")) rel += "index.html";
  let file = path.join(EXPORT_DIR, rel);
  if (!file.startsWith(EXPORT_DIR)) {
    res.writeHead(403);
    return res.end("forbidden");
  }
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    const withHtml = `${file}.html`;
    file = fs.existsSync(withHtml) ? withHtml : path.join(EXPORT_DIR, "index.html");
  }
  if (!fs.existsSync(file)) {
    res.writeHead(404);
    return res.end("not found");
  }
  res.writeHead(200, {
    "content-type": TYPES[path.extname(file)] || "application/octet-stream",
    // Never let the harness serve a stale chunk — the whole point is testing fresh bytes.
    "cache-control": "no-store",
  });
  fs.createReadStream(file).pipe(res);
});

if (!fs.existsSync(path.join(EXPORT_DIR, "index.html"))) {
  console.error(`No built export at ${EXPORT_DIR} — build and copy the storyboard app first.`);
  process.exit(1);
}

server.listen(PORT, "127.0.0.1", () => {
  console.log(`storyboard harness → http://127.0.0.1:${PORT}/storyboard/  (scenario=${scenario})`);
});

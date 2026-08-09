// predeploy-check.mjs — DEPLOY GATE. A redeploy restarts the containers and KILLS any ACTIVE
// render/gen job (→ stall → re-run → re-charge). Run this BEFORE `git push …`:
//
//   railway run -s video-worker node video/predeploy-check.mjs            # exit 0 = idle/safe, 1 = busy
//   railway run -s video-worker node video/predeploy-check.mjs --wait     # QUEUE the deploy: poll until idle
//   railway run -s video-worker node video/predeploy-check.mjs --wait=900 # …with a 900s cap
//
// Only ACTIVE jobs block — WAITING/DELAYED jobs survive a restart (BullMQ re-picks them), so they
// don't need to drain. Pair with the worker's SIGTERM graceful-drain (worker-entry.mjs).
//
// ── WHAT IT COVERS, AND WHY IT IS NOT A HAND-WRITTEN LIST ────────────────────────────────────────
// This gate previously iterated a hardcoded [AUDIO, VISUAL, CHECK, STITCH]. When the BullMQ trigger
// queues `video-clip` and `video-recipe` were added to QUEUE in connection.mjs, this list was not
// updated with them, so for as long as that lasted an ACTIVE clip or recipe job passed the gate and
// was killed by the restart — the exact failure the gate exists to prevent. A second hand-maintained
// copy of a list is a defect waiting for the next queue.
//
// So coverage is now derived, in three layers, and EVERY layer blocks on active > 0:
//   1. VIDEO      — `Object.values(QUEUE)`, so a queue added to connection.mjs is gated automatically.
//   2. NARRATION  — the python-side queues (python/narration_worker.py). A deploy restarts
//                   narration-worker too, and killing a narasi job mid-flight re-runs the LLM
//                   generation, so it belongs in the same gate rather than in someone's memory.
//                   NARRATION_RESUME_ENABLED bounds that cost; it does not remove it.
//   3. DISCOVERED — anything else answering `bull:*:meta` on the same Redis. All services share one
//                   REDIS_URL, so this sees queues this file has never heard of. An idle unknown
//                   queue costs nothing (0 active never blocks); a BUSY one stops the deploy and is
//                   named in the output, which is the correct default for a gate.
//
// Set PREDEPLOY_SCAN_DISCOVERED=0 to disable layer 3 (e.g. if an unrelated product ever shares this
// Redis with a permanently-active queue). It is opt-OUT, and the skipped queues are printed rather
// than silently dropped — a gate that quietly narrows itself is worse than no gate.
import { Queue } from "bullmq";
import { pathToFileURL } from "node:url";
import { makeConnection, QUEUE } from "./connection.mjs";

// Layer 1: straight from the single source of truth. No second list to forget.
const VIDEO_QUEUES = Object.values(QUEUE);

// Layer 2: python-side. `narration_worker.py` and `narration_api.py` both resolve the live name as
// os.environ["NARRATION_QUEUE"] with default "narration", so honour the same variable here — and keep
// the historical names, which are still present in Redis and would become live again if the variable
// were ever pointed back at one. They are empty today, and an empty queue never blocks.
export const NARRATION_QUEUES = [
  ...new Set([process.env.NARRATION_QUEUE || "narration", "narration", "narasi", "narration-jobs"]),
];

export const KNOWN_QUEUES = [...new Set([...VIDEO_QUEUES, ...NARRATION_QUEUES])];

const scanDiscovered = String(process.env.PREDEPLOY_SCAN_DISCOVERED ?? "1") !== "0";

/** Every queue with a BullMQ meta key on this Redis. BullMQ writes `bull:<name>:meta` per queue. */
export async function discoverQueues(conn) {
  const found = new Set();
  let cursor = "0";
  do {
    const [next, batch] = await conn.scan(cursor, "MATCH", "bull:*:meta", "COUNT", 500);
    cursor = next;
    // `bull:<name>:meta` — the name itself cannot contain ":" (BullMQ forbids it, see connection.mjs),
    // so index 1 is the whole queue name and this split is unambiguous.
    for (const key of batch) found.add(key.split(":")[1]);
  } while (cursor !== "0");
  return [...found].sort();
}

async function activeSnapshot() {
  const conn = makeConnection();
  const detail = {};
  const skipped = [];
  let active = 0;
  try {
    let names = KNOWN_QUEUES;
    if (scanDiscovered) {
      const discovered = await discoverQueues(conn);
      names = [...new Set([...KNOWN_QUEUES, ...discovered])];
    } else {
      // opt-out: say exactly what is no longer being looked at
      skipped.push(...(await discoverQueues(conn)).filter((n) => !KNOWN_QUEUES.includes(n)));
    }
    for (const name of names) {
      const q = new Queue(name, { connection: conn });
      const c = await q.getJobCounts("active", "waiting", "delayed");
      detail[name] = c;
      active += c.active || 0;
      await q.close();
    }
  } finally { await conn.quit().catch(() => {}); }
  return { active, detail, skipped, checked: Object.keys(detail) };
}

// Only run the gate when executed as a script. Without this, importing the module to test
// KNOWN_QUEUES would open Redis, poll, and call process.exit() out from under the test runner.
const isMain = import.meta.url === pathToFileURL(process.argv[1] || "").href;

if (isMain) {
  const waitArg = process.argv.find((a) => a === "--wait" || a.startsWith("--wait="));
  const wait = !!waitArg;
  const maxSec = Number((waitArg || "").split("=")[1]) || 1200;
  const pollSec = 10;

  let waited = 0;
  for (;;) {
    const { active, detail, skipped, checked } = await activeSnapshot();
    if (skipped.length) {
      console.log(`⚠️  PREDEPLOY_SCAN_DISCOVERED=0 — NOT checking ${skipped.length} queue(s): ${skipped.join(", ")}`);
    }
    if (active === 0) {
      console.log(`✅ no ACTIVE jobs across ${checked.length} queue(s) — SAFE TO DEPLOY`);
      console.log(`   checked: ${checked.join(", ")}`);
      process.exit(0);
    }
    const busy = Object.entries(detail).filter(([, c]) => (c.active || 0) > 0)
      .map(([n, c]) => `${n}=${c.active}`).join(", ");
    console.log(`⏳ ${active} ACTIVE job(s) in ${busy}`);
    console.log(`   full: ${JSON.stringify(detail)}`);
    if (!wait) { console.log("❌ NOT SAFE — a job is running. Re-run with --wait to QUEUE the deploy until idle."); process.exit(1); }
    if (waited >= maxSec) { console.log(`❌ still busy after ${maxSec}s — aborting (deploy NOT performed).`); process.exit(1); }
    await new Promise((r) => setTimeout(r, pollSec * 1000)); waited += pollSec;
  }
}

// predeploy-check.mjs — DEPLOY GATE for the video-worker. A redeploy restarts the container and KILLS
// any ACTIVE render/gen job (→ stall → re-run → re-charge). Run this BEFORE `git push …:main`:
//
//   railway run -s video-worker node video/predeploy-check.mjs            # exit 0 = idle/safe, 1 = busy
//   railway run -s video-worker node video/predeploy-check.mjs --wait     # QUEUE the deploy: poll until idle
//   railway run -s video-worker node video/predeploy-check.mjs --wait=900 # …with a 900s cap
//
// Only ACTIVE jobs block — WAITING/DELAYED jobs survive a restart (BullMQ re-picks them), so they
// don't need to drain. Pair with the worker's SIGTERM graceful-drain (worker-entry.mjs).
import { Queue } from "bullmq";
import { makeConnection, QUEUE } from "./connection.mjs";

async function activeSnapshot() {
  const conn = makeConnection();
  const detail = {};
  let active = 0;
  try {
    for (const name of [QUEUE.AUDIO, QUEUE.VISUAL, QUEUE.CHECK, QUEUE.STITCH]) {
      const q = new Queue(name, { connection: conn });
      const c = await q.getJobCounts("active", "waiting", "delayed");
      detail[name] = c; active += c.active || 0;
      await q.close();
    }
  } finally { await conn.quit().catch(() => {}); }
  return { active, detail };
}

const waitArg = process.argv.find((a) => a === "--wait" || a.startsWith("--wait="));
const wait = !!waitArg;
const maxSec = Number((waitArg || "").split("=")[1]) || 1200;
const pollSec = 10;

let waited = 0;
for (;;) {
  const { active, detail } = await activeSnapshot();
  if (active === 0) { console.log("✅ no ACTIVE jobs — SAFE TO DEPLOY"); process.exit(0); }
  console.log(`⏳ ${active} ACTIVE job(s): ${JSON.stringify(detail)}`);
  if (!wait) { console.log("❌ NOT SAFE — a render/gen job is running. Re-run with --wait to QUEUE the deploy until idle."); process.exit(1); }
  if (waited >= maxSec) { console.log(`❌ still busy after ${maxSec}s — aborting (deploy NOT performed).`); process.exit(1); }
  await new Promise((r) => setTimeout(r, pollSec * 1000)); waited += pollSec;
}

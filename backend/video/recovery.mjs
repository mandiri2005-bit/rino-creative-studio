// ─────────────────────────────────────────────────────────────────────────────
// video/recovery.mjs — GUARANTEE: no job ever ends "charged but stuck".
//
// THE GAP this closes: the `audio` and `visual` workers each have an `.on("failed")`
// safety-net, but the `stitch` worker had NONE. So a stitch killed by a deploy/restart
// or a lock-stall ("job stalled more than allowable limit") left the job at status
// "stitching" forever — never re-run, never refunded. The user was charged and got no
// video. (Observed: vid_mqmsr0fw / vid_mqmgl2b0.)
//
// This adds three converging mechanisms, all ADDITIVE (no render-engine change):
//   1) onStitchFailed()  — wired to stitch.on("failed"): re-dispatch (force-complete)
//                          up to WB_STITCH_MAX_RECOVERY, then fail + refund.
//   2) boot scan         — on worker start, recover jobs orphaned by the restart that
//                          just happened (the common deploy case).
//   3) periodic reaper   — every WB_REAPER_MS, recover jobs orphaned while alive.
//
// Recovery REUSES the proven convergence path: clear the stitch claim-guard, then
// enqueue a `check` → orchestrator.advance() re-stitches (if scenes ready), re-dispatches
// a missing scene batch, or fails+refunds. The only NEW policy here is the bounded
// retry counter → guaranteed refund when a job truly can't complete. Refund is
// idempotent server-side (op_id video-refund:<job>), so a double-fire never double-pays.
// ─────────────────────────────────────────────────────────────────────────────
import { makeConnection } from "./connection.mjs";

const TERMINAL = new Set(["done", "failed", "canceled"]); // a finished job — never touch

export function startRecovery(deps) {
  if (/^(0|false|no|off)$/i.test(String(process.env.WB_RECOVERY ?? "1"))) {
    return { onStitchFailed() {}, async stop() {} };
  }
  const MAX = Math.max(0, Number(process.env.WB_STITCH_MAX_RECOVERY || 2));
  const REAPER_MS = Math.max(30000, Number(process.env.WB_REAPER_MS || 180000));
  // Boot scan fires AFTER BullMQ's own stall window (stalledInterval 60s) so BullMQ re-delivers a
  // stalled stitch FIRST (then it shows as a live job and we skip it) — avoids a double-stitch race.
  const BOOT_DELAY_MS = Math.max(0, Number(process.env.WB_RECOVERY_BOOT_DELAY_MS || 90000));
  const conn = deps.conn || makeConnection(); // injectable for tests
  const queues = [deps.queues?.audio, deps.queues?.visual, deps.queues?.check, deps.queues?.stitch].filter(Boolean);

  // jobIds that currently have a real BullMQ job somewhere (active/waiting/delayed/paused).
  // An actively-RENDERING stitch is "active" here → it is NOT an orphan and is left alone.
  async function liveJobIds() {
    const live = new Set();
    for (const q of queues) {
      try {
        const jobs = await q.getJobs(["active", "waiting", "delayed", "paused"], 0, 1000);
        for (const j of jobs) if (j?.data?.jobId) live.add(j.data.jobId);
      } catch { /* best effort */ }
    }
    return live;
  }

  // Re-drive ONE job toward completion, or refund it if it has exhausted its retries.
  async function recover(jobId, reason) {
    if (!jobId) return;
    // single-flight per job (boot + reaper + failed-handler can race)
    if ((await conn.set(`vjob:${jobId}:recovering`, "1", "EX", 90, "NX")) !== "OK") return;
    try {
      const meta = await deps.store.getMeta(jobId);
      if (!meta || TERMINAL.has(meta.status)) return; // already finished — nothing to do
      const n = await conn.incr(`vjob:${jobId}:recoveries`);
      await conn.expire(`vjob:${jobId}:recoveries`, 24 * 3600);
      if (n > MAX) {
        console.warn(`[recover] ${jobId} UNRECOVERABLE after ${MAX} retries (${reason}) → failed + refund`);
        await deps.store.setStatus(jobId, "failed", { error: `unrecoverable: ${String(reason || "").slice(0, 120)}` });
        await deps.credits?.refundJob?.(meta.tenantId, jobId); // idempotent
        return;
      }
      // Clear the stitch claim-guard so advance()'s tryClaimStitch can re-claim & re-dispatch,
      // then let the EXISTING convergence decide (re-stitch / re-dispatch scene / fail+refund).
      await conn.del(`vjob:${jobId}:stitch`).catch(() => {});
      await deps.queues.check.add("check", { jobId });
      console.warn(`[recover] ${jobId} re-dispatched (attempt ${n}/${MAX}, status=${meta.status}, reason=${reason})`);
    } finally {
      await conn.del(`vjob:${jobId}:recovering`).catch(() => {});
    }
  }

  // Find in-flight jobs (NOT done/failed/canceled) that have NO live queue job → orphans.
  async function scan(reason) {
    let keys = [];
    try { keys = await conn.keys("vjob:*"); } catch { return; }
    const metaKeys = keys.filter((k) => /^vjob:[^:]+$/.test(k)); // skip :scene: / :stitch / :recovering / :dispatched
    if (!metaKeys.length) return;
    const live = await liveJobIds();
    let recovered = 0;
    for (const k of metaKeys) {
      const jobId = k.slice("vjob:".length);
      if (live.has(jobId)) continue; // has a real queue job (incl. an active render) → not orphaned
      let meta; try { meta = await deps.store.getMeta(jobId); } catch { continue; }
      if (!meta || TERMINAL.has(meta.status)) continue;
      await recover(jobId, reason);
      recovered++;
    }
    if (recovered) console.warn(`[recover] ${reason} scan re-drove ${recovered} orphaned job(s)`);
  }

  const bootT = setTimeout(() => scan("boot").catch(() => {}), BOOT_DELAY_MS);
  const iv = setInterval(() => scan("reaper").catch(() => {}), REAPER_MS);
  if (iv.unref) iv.unref();
  console.log(`[recover] active — stitch failed-handler + boot scan + reaper (max=${MAX}, reaper=${REAPER_MS}ms)`);

  return {
    onStitchFailed(jobId, reason) { recover(jobId, `stitch-failed: ${reason || ""}`).catch(() => {}); },
    // exposed for tests / manual use
    _recover: recover,
    _scan: scan,
    async stop() { clearTimeout(bootT); clearInterval(iv); await conn.quit().catch(() => {}); },
  };
}

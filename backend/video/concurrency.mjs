// backend/video/concurrency.mjs
// ───────────────────────────────────────────────────────────────────────────
// Per-plan parallel-job limiter (Phase 3). One Redis HASH per tenant holds live
// slot tokens {slotId -> expiryEpoch}; acquire() reaps expired tokens, counts the
// rest, and admits only while count < the plan cap. Atomic via a single Lua script
// so two simultaneous submits can't both pass a stale count. A per-slot TTL means a
// crashed worker's slot self-heals (reaped on the next acquire). FAIL-OPEN: a Redis
// blip allows the job — concurrency is a fairness/abuse cap, NOT a money gate.
//
// One video ASSEMBLY = one slot (acquired at /api/video/assemble, released when the
// job goes terminal via store.setStatus). Caps: Free 1 / Starter 2 / Pro 4 / Studio 8.
// ───────────────────────────────────────────────────────────────────────────
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { sharedConnection } from "./connection.mjs";

const _DEFAULT_CAPS = { free: 1, starter: 2, pro: 4, enterprise: 8 };
function _loadCaps() {
  try {
    const here = dirname(fileURLToPath(import.meta.url));
    const raw = readFileSync(join(here, "..", "..", "config", "pricing.json"), "utf8");
    return { ..._DEFAULT_CAPS, ...((JSON.parse(raw).concurrency_caps) || {}) };
  } catch { return { ..._DEFAULT_CAPS }; }
}
const CAPS = _loadCaps();
const SLOT_TTL = Number(process.env.CONC_SLOT_TTL ?? 1800);   // orphan slot reaped after 30 min

export function capFor(plan) { return CAPS[plan] ?? CAPS.free; }

// reap expired tokens → count live → idempotent re-acquire → admit if under cap
const _ACQUIRE = `
local key=KEYS[1]; local cap=tonumber(ARGV[1]); local sid=ARGV[2]; local now=tonumber(ARGV[3]); local ttl=tonumber(ARGV[4])
local s=redis.call('HGETALL', key); local n=0
for i=1,#s,2 do if tonumber(s[i+1])<now then redis.call('HDEL', key, s[i]) else n=n+1 end end
if redis.call('HEXISTS', key, sid)==1 then redis.call('HSET', key, sid, now+ttl); return 1 end
if n>=cap then return 0 end
redis.call('HSET', key, sid, now+ttl); redis.call('EXPIRE', key, 86400); return 1`;
const _RELEASE = `redis.call('HDEL', KEYS[1], ARGV[1]); return 1`;

const _slotKey = (t) => `conc:${t}:slots`;

// Returns true if admitted (slot held), false if the tenant is at its cap.
export async function acquire(tenantId, plan, slotId) {
  if (!tenantId || !slotId) return true;                  // no tenant → not capped (gated elsewhere)
  try {
    const r = sharedConnection();
    const now = Math.floor(Date.now() / 1000);
    const ok = await r.eval(_ACQUIRE, 1, _slotKey(tenantId), capFor(plan), slotId, now, SLOT_TTL);
    return Number(ok) === 1;
  } catch (e) { console.warn("[conc] acquire fail-open:", e?.message); return true; }   // fail OPEN
}

// Idempotent: releasing an absent slot is a no-op (HDEL).
export async function release(tenantId, slotId) {
  if (!tenantId || !slotId) return;
  try { await sharedConnection().eval(_RELEASE, 1, _slotKey(tenantId), slotId); }
  catch (e) { console.warn("[conc] release:", e?.message); }
}

// ── GLOBAL admission cap (Change 2) ──────────────────────────────────────────
// The per-tenant cap above is PER TENANT, so 100 DISTINCT users each hold their own
// slot and ALL get admitted → a launch spike piles 100 assemblies onto the single
// worker and they starve each other behind the 2-wide stitch queue (a hours-deep,
// frozen-looking backlog). This is ONE global in-flight-assembly cap: above it,
// /assemble returns a friendly 429 ("lagi ramai") so the spike queues at the door
// instead. Same reap/per-slot-TTL/FAIL-OPEN semantics as the per-tenant limiter.
// Default 0 = DISABLED — set VIDEO_MAX_INFLIGHT_JOBS (e.g. 20) on the API service.
const GLOBAL_MAX = Number(process.env.VIDEO_MAX_INFLIGHT_JOBS ?? 0);
const _globalKey = () => `conc:global:slots`;
export function globalMax() { return GLOBAL_MAX; }

export async function acquireGlobal(slotId) {
  if (!GLOBAL_MAX || GLOBAL_MAX < 1 || !slotId) return true;     // disabled → always admit
  try {
    const r = sharedConnection();
    const now = Math.floor(Date.now() / 1000);
    const ok = await r.eval(_ACQUIRE, 1, _globalKey(), GLOBAL_MAX, slotId, now, SLOT_TTL);
    return Number(ok) === 1;
  } catch (e) { console.warn("[conc] global acquire fail-open:", e?.message); return true; }  // fail OPEN
}
// Idempotent (HDEL). Released alongside the per-tenant slot when a job goes terminal.
// Early-return when the cap is off → the feature is fully Redis-silent by default (no stray HDEL).
export async function releaseGlobal(slotId) {
  if (!slotId || GLOBAL_MAX < 1) return;
  try { await sharedConnection().eval(_RELEASE, 1, _globalKey(), slotId); }
  catch (e) { console.warn("[conc] global release:", e?.message); }
}

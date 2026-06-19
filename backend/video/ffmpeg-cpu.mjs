// ─────────────────────────────────────────────────────────────────────────────
// video/ffmpeg-cpu.mjs — box-level CPU guard for ffmpeg ENCODES.
//
// The BullMQ queues already separate CPU (video-stitch) from I/O (video-visual/
// video-audio) — that part is correct and untouched. What was UNBOUNDED was the
// *cross-product* of concurrent encoders: one long video fans out to
// VIDEO_RENDER_CONCURRENCY per-scene ffmpeg processes, times the stitch worker
// concurrency, times replicas — and with no -threads each encoder grabbed every
// core → ~8–20× thread oversubscription → scheduler thrash → everything crawls.
//
// This in-memory, per-process semaphore caps how many ffmpeg ENCODES run at once
// on THIS box, so the box never thrashes regardless of how many videos stitch
// together. Per-process is the right scope: CPU is per-replica, so a Redis-global
// cap would wrongly throttle replicas that each own their own cores.
//
// Sizing rule: (VIDEO_THREADS + VIDEO_FILTER_THREADS) × FFMPEG_ENCODER_SLOTS ≈ vCPU.
// Default 2 × (2 + 2) = 8 on the 8-vCPU worker — full utilisation, no oversubscription.
// (-threads alone is NOT enough: the filtergraph — zoompan/scale/xfade — uses a
// SEPARATE thread pool, capped by -filter_complex_threads in ffmpeg.mjs.)
//
// ESCAPE HATCH: set FFMPEG_ENCODER_SLOTS=0 (or blank) → unlimited pass-through,
// which restores pre-fix behaviour. Env changes restart the worker (Railway), so
// this is a no-rebuild rollback. Only ENCODES are wrapped — never the concat-copy
// (-c copy, no encode) or ffprobe.
// ─────────────────────────────────────────────────────────────────────────────

function readSlots() {
  const raw = process.env.FFMPEG_ENCODER_SLOTS;
  if (raw === undefined || raw === "") return 2;   // conservative default for 8 vCPU
  const n = Number(raw);
  return Number.isFinite(n) ? n : 2;
}

let _max = readSlots();      // <= 0 → unlimited (escape hatch)
let _active = 0;
const _waiters = [];

function _acquire() {
  if (_max <= 0) return Promise.resolve();                 // unlimited: no gating
  if (_active < _max) { _active++; return Promise.resolve(); }
  return new Promise((resolve) => _waiters.push(resolve)); // queue until a slot frees
}

function _release() {
  if (_max <= 0) return;
  const next = _waiters.shift();
  if (next) next();                       // hand the slot straight to the next waiter
  else _active = Math.max(0, _active - 1);
}

/**
 * Run fn() while holding one encoder slot. Releases in `finally`, so an encode that
 * throws or times out can NEVER leak a slot — the exact failure mode this fix is also
 * closing on the I/O side (a hung fetch holding a BullMQ slot).
 */
export async function withEncoderSlot(fn) {
  await _acquire();
  try {
    return await fn();
  } finally {
    _release();
  }
}

// ── Test / introspection helpers (not used in production paths) ──
export function _encoderSlotStats() {
  return { max: _max, active: _active, waiting: _waiters.length };
}
export function _setEncoderSlotsForTest(n) {
  _max = Number(n);
  _active = 0;
  _waiters.length = 0;
}

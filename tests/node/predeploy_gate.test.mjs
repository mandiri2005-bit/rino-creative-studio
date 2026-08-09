// ─────────────────────────────────────────────────────────────────────────────
// predeploy_gate.test.mjs — the deploy gate must cover EVERY queue a deploy can kill.
//
// The bug this locks down: predeploy-check.mjs iterated a hand-written
// [AUDIO, VISUAL, CHECK, STITCH] while connection.mjs's QUEUE had grown to six. For as
// long as that lasted, an ACTIVE `video-clip` or `video-recipe` job passed the gate and
// was killed by the redeploy — the exact stall → re-run → re-charge the gate exists to
// prevent. Both trigger queues are live in production (VIDEO_BULLMQ_ENABLED=1,
// RECIPE_BULLMQ_ENABLED=1), so this was not theoretical.
//
// The fix is derivation, not a longer list, and the first test is what enforces that:
// coverage is asserted against `Object.values(QUEUE)`, so adding a seventh queue to
// connection.mjs without touching the gate fails HERE rather than in production.
//
// Suite C needs a real Redis. Start one and point the tests at it:
//   redis-server --port 6399 --save '' --daemonize yes
//   PREDEPLOY_TEST_REDIS_URL=redis://127.0.0.1:6399 node --test tests/node/predeploy_gate.test.mjs
// Without that variable suite C skips; A and B are hermetic and always run.
// ─────────────────────────────────────────────────────────────────────────────
import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import { QUEUE } from "../../backend/video/connection.mjs";
import { KNOWN_QUEUES, NARRATION_QUEUES, discoverQueues } from "../../backend/video/predeploy-check.mjs";

describe("deploy gate — queue coverage is derived, never hand-listed", () => {
  test("NON-VACUITY: QUEUE really does define more than the historical four", () => {
    const names = Object.values(QUEUE);
    assert.ok(names.length > 4,
      `QUEUE has ${names.length} entries; if it ever drops to 4 this suite is testing nothing`);
    for (const historical of ["video-audio", "video-visual", "video-check", "video-stitch"]) {
      assert.ok(names.includes(historical), `${historical} must still be defined`);
    }
  });

  test("the gate covers EVERY queue in QUEUE — this is the regression guard", () => {
    const missing = Object.values(QUEUE).filter((n) => !KNOWN_QUEUES.includes(n));
    assert.deepEqual(missing, [],
      `queues defined in connection.mjs but not gated: ${missing.join(", ")}. ` +
      "Coverage must derive from Object.values(QUEUE), not a second hand-maintained list.");
  });

  test("the two queues the old gate missed are specifically covered", () => {
    // Named explicitly, because these are the ones that were actually unguarded.
    assert.ok(KNOWN_QUEUES.includes("video-clip"), "video-clip must be gated");
    assert.ok(KNOWN_QUEUES.includes("video-recipe"), "video-recipe must be gated");
    assert.equal(QUEUE.VIDEOCLIP, "video-clip");
    assert.equal(QUEUE.RECIPE, "video-recipe");
  });

  test("narration queues are gated too — the same deploy restarts that worker", () => {
    for (const n of ["narration", "narasi", "narration-jobs"]) {
      assert.ok(KNOWN_QUEUES.includes(n), `${n} must be gated`);
    }
  });

  test("KNOWN_QUEUES has no duplicates", () => {
    assert.equal(new Set(KNOWN_QUEUES).size, KNOWN_QUEUES.length);
  });

  test("the live narration queue name follows NARRATION_QUEUE, as python resolves it", async () => {
    // python/narration_worker.py and narration_api.py both use
    // os.environ.get("NARRATION_QUEUE", "narration"); the gate must not hardcode past that.
    assert.ok(NARRATION_QUEUES.includes("narration"), "default must be covered");

    const saved = process.env.NARRATION_QUEUE;
    process.env.NARRATION_QUEUE = "narration-somewhere-else";
    try {
      // cache-bust: NARRATION_QUEUES is computed once at module load
      const reloaded = await import("../../backend/video/predeploy-check.mjs?envcase=1");
      assert.ok(reloaded.NARRATION_QUEUES.includes("narration-somewhere-else"),
        "a redirected NARRATION_QUEUE must be gated");
      assert.ok(reloaded.KNOWN_QUEUES.includes("narration-somewhere-else"));
      assert.ok(reloaded.NARRATION_QUEUES.includes("narration"),
        "…and the historical names stay, so an old queue draining is still seen");
    } finally {
      if (saved === undefined) delete process.env.NARRATION_QUEUE;
      else process.env.NARRATION_QUEUE = saved;
    }
  });
});

describe("deploy gate — discoverQueues", () => {
  /** Minimal ioredis stand-in: only `scan` is used, and it is driven cursor by cursor. */
  const fakeConn = (pages) => {
    let i = 0;
    return {
      calls: [],
      async scan(cursor, ...args) {
        this.calls.push([cursor, ...args]);
        const page = pages[i++];
        return [i < pages.length ? String(i) : "0", page];
      },
    };
  };

  test("parses the queue name out of bull:<name>:meta", async () => {
    const conn = fakeConn([["bull:video-audio:meta", "bull:video-clip:meta"]]);
    assert.deepEqual(await discoverQueues(conn), ["video-audio", "video-clip"]);
  });

  test("follows the SCAN cursor to the end — a queue on page 2 is not lost", async () => {
    const conn = fakeConn([
      ["bull:video-audio:meta"],
      ["bull:video-recipe:meta"],
      ["bull:narration:meta"],
    ]);
    const found = await discoverQueues(conn);
    assert.deepEqual(found, ["narration", "video-audio", "video-recipe"]);
    assert.equal(conn.calls.length, 3, "must keep scanning until the cursor returns to 0");
    assert.equal(conn.calls[0][0], "0", "first call starts at cursor 0");
    assert.ok(conn.calls[0].includes("bull:*:meta"), "must match on the BullMQ meta key");
  });

  test("de-duplicates names seen on more than one page", async () => {
    const conn = fakeConn([["bull:video-audio:meta"], ["bull:video-audio:meta"]]);
    assert.deepEqual(await discoverQueues(conn), ["video-audio"]);
  });

  test("an empty Redis yields an empty list, not a throw", async () => {
    assert.deepEqual(await discoverQueues(fakeConn([[]])), []);
  });
});

describe("deploy gate — discovery against a real Redis", {
  skip: process.env.PREDEPLOY_TEST_REDIS_URL ? false : "PREDEPLOY_TEST_REDIS_URL not set",
}, () => {
  let conn;
  let queues = [];

  before(async () => {
    const { default: Redis } = await import("ioredis");
    const { Queue } = await import("bullmq");
    conn = new Redis(process.env.PREDEPLOY_TEST_REDIS_URL, {
      maxRetriesPerRequest: null, enableReadyCheck: false,
    });
    await conn.flushdb();
    // two the gate knows, and one it has never heard of — layer 3's whole reason to exist
    for (const name of ["video-clip", "narration", "some-future-product-queue"]) {
      const q = new Queue(name, { connection: conn });
      await q.waitUntilReady();          // this is what writes bull:<name>:meta
      queues.push(q);
    }
  });

  after(async () => {
    for (const q of queues) await q.close().catch(() => {});
    if (conn) { await conn.flushdb().catch(() => {}); await conn.quit().catch(() => {}); }
  });

  test("finds real BullMQ queues, including one outside KNOWN_QUEUES", async () => {
    const found = await discoverQueues(conn);
    assert.ok(found.includes("video-clip"), "a known queue must be discovered");
    assert.ok(found.includes("narration"), "a narration queue must be discovered");
    assert.ok(found.includes("some-future-product-queue"),
      "a queue this file has never heard of must still be discovered — that is layer 3's job");
  });

  test("an unknown queue is genuinely outside the hand-known set", () => {
    assert.ok(!KNOWN_QUEUES.includes("some-future-product-queue"),
      "if this ever becomes known the previous test stops proving discovery");
  });
});

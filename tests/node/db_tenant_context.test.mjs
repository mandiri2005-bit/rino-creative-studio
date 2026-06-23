// db_tenant_context.test.mjs — proves db.setTenantContext() sets a
// TRANSACTION-LOCAL RLS tenant context (set_config is_local=true) that resets on
// COMMIT, so it never leaks across pooled (Neon/PgBouncer) connections. This is
// the guard whose earlier `SET LOCAL x = $1` form silently 500'd.
//
// Integration test — needs a reachable Postgres. Provide TEST_DATABASE_URL; the
// test is SKIPPED otherwise so the unit suite still runs without a DB.
//   TEST_DATABASE_URL="postgres://…" node --test ../tests/node/db_tenant_context.test.mjs
import { test, after } from "node:test";
import assert from "node:assert/strict";

const URL = process.env.TEST_DATABASE_URL;
process.env.DATABASE_POOL_URL_DEV ??= URL || "postgres://u:p@localhost:5432/db";
process.env.REDIS_URL ??= "redis://localhost:6379";

const { setTenantContext, pool } = await import("../../backend/db.js");
const { redis } = await import("../../backend/redis.js");
after(() => { try { redis.disconnect(); } catch {} try { pool.end(); } catch {} });

test("setTenantContext is transaction-local and resets after COMMIT",
  { skip: URL ? false : "set TEST_DATABASE_URL to run this integration test" },
  async () => {
    const TID = "11111111-1111-1111-1111-111111111111";
    const client = await pool.connect();
    try {
      await client.query("BEGIN");
      await setTenantContext(client, TID);
      const inTx = (await client.query("SELECT current_setting('app.current_tenant_id', true) v")).rows[0].v;
      assert.equal(inTx, TID, "context must be visible within the SAME transaction");
      await client.query("COMMIT");
      // is_local=true → reset when the txn ends; next (autocommit) stmt sees default.
      const afterTx = (await client.query("SELECT current_setting('app.current_tenant_id', true) v")).rows[0].v;
      assert.ok(afterTx === "" || afterTx == null, `expected reset after COMMIT, got '${afterTx}'`);
    } finally {
      client.release();
    }
  });

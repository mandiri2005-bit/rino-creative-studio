// cr29_item4_assent_gate.test.mjs — CR-29 item 4 (PLAN §S10.G3.1-i, migration 0085).
//
// ENVIRONMENT: real PostgreSQL. Set L2C_TEST_DATABASE_URL to a DISPOSABLE cluster
// with all migrations applied. The suite SKIPS (it does not pass) when unset — a
// green run with no database would be vacuous.
//
//   node --test ../tests/node/cr29_item4_assent_gate.test.mjs
//
// COVERAGE PROVENANCE, stated so it is not misread:
//   * RECORDED in MATRIX-045 T81: shared lock (writer AND gate), latest-event
//     semantics, composite FKs, function-only writes + privilege non-vacuity,
//     two-session commit orders, provider call strictly after COMMIT.
//   * NOT YET IN MATRIX — technical additions carried here until a MATRIX row is
//     recorded: tenant-GUC negatives, actor-authority negatives, shadow-mode
//     never-refuses. Marked [MATRIX-PENDING] in the test names.
import { test, before, after, describe } from "node:test";
import assert from "node:assert/strict";
import pg from "pg";

const URL = process.env.L2C_TEST_DATABASE_URL;
const SKIP = !URL;
const SHA_A = "a".repeat(64);
const T1 = "11111111-1111-1111-1111-111111111111";
const T2 = "22222222-2222-2222-2222-222222222222";
const U1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const U2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

let pool;
before(async () => { if (!SKIP) pool = new pg.Pool({ connectionString: URL, max: 6 }); });
after(async () => { if (pool) await pool.end(); });

/** One transaction with the tenant GUC set, mirroring backend/db.js::query(). */
async function tx(tenantId, fn) {
  const c = await pool.connect();
  try {
    await c.query("BEGIN");
    await c.query("SELECT set_config('app.current_tenant_id', $1, true)", [tenantId ?? ""]);
    const r = await fn(c);
    await c.query("COMMIT");
    return r;
  } catch (e) { await c.query("ROLLBACK").catch(() => {}); throw e; }
  finally { c.release(); }
}

async function seed() {
  const c = await pool.connect();
  try {
    await c.query("SET session_replication_role = replica");
    await c.query("DELETE FROM topup_checkout_intents");
    await c.query("DELETE FROM tos_acceptances");
    await c.query("SET session_replication_role = origin");
    await c.query(
      `INSERT INTO tenants (id,name,slug,email,plan) VALUES
         ($1,'FIXTURE-A','fixture-a','a@example.test','starter'),
         ($2,'FIXTURE-B','fixture-b','b@example.test','starter')
       ON CONFLICT DO NOTHING`, [T1, T2]);
    await c.query(
      `INSERT INTO users (id,tenant_id,email) VALUES ($1,$3,'a@example.test'),($2,$4,'b@example.test')
       ON CONFLICT DO NOTHING`, [U1, U2, T1, T2]);
    await c.query(
      `INSERT INTO tos_versions (tos_version,published_at,effective_at)
       VALUES ('FIXTURE-v1', now()-interval '1 day', now()-interval '1 day')
       ON CONFLICT DO NOTHING`);
    await c.query(
      `INSERT INTO tos_version_artifacts
         (tos_version,locale,artifact_sha256,archive_uri,byte_size,content_type,archived_at)
       VALUES ('FIXTURE-v1','id',$1,'fixture://a',10,'text/html',now())
       ON CONFLICT DO NOTHING`, [SHA_A]);
    await c.query(
      `INSERT INTO tos_artifact_publication_events
         (tos_version,locale,artifact_sha256,event,public_route,occurred_at)
       SELECT 'FIXTURE-v1','id',$1,'published','/legal/fixture',now()
        WHERE NOT EXISTS (SELECT 1 FROM tos_artifact_publication_events)`, [SHA_A]);
  } finally { c.release(); }
}

const assent = (c, tenant, actor, event) => c.query(
  `SELECT * FROM tos_record_assent($1,$2,'FIXTURE-v1','id',$3,$4,'FIXTURE-surface',
                                   'FIXTURE-rule-not-production-policy','FIXTURE-deploy')`,
  [tenant, actor, SHA_A, event]);

describe("CR-29 item 4 — assent gate", { skip: SKIP ? "L2C_TEST_DATABASE_URL not set" : false }, () => {

  test("NON-VACUITY: fixtures seed and a shadow intent is created", async () => {
    await seed();
    const r = await tx(T1, (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1]));
    assert.equal(r.rows[0].gate_active_at_create, false);
    assert.equal(r.rows[0].tos_version, null, "shadow rows carry no assent pin");
  });

  test("[MATRIX-PENDING] shadow mode NEVER refuses, even with a latest 'rejected'", async () => {
    await seed();
    await tx(T1, (c) => assent(c, T1, U1, "rejected"));
    const r = await tx(T1, (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1]));
    assert.ok(r.rows[0].checkout_intent_id, "shadow must not gate the rail");
  });

  test("latest-event semantics: an EXISTS-style predicate would be wrong", async () => {
    await seed();
    await tx(T1, (c) => assent(c, T1, U1, "accepted"));
    await tx(T1, (c) => assent(c, T1, U1, "rejected"));
    const r = await tx(T1, (c) => c.query(
      `SELECT event FROM tos_acceptances WHERE tenant_id=$1 ORDER BY event_seq DESC LIMIT 1`, [T1]));
    assert.equal(r.rows[0].event, "rejected");
    const anyAccepted = await tx(T1, (c) => c.query(
      `SELECT EXISTS(SELECT 1 FROM tos_acceptances WHERE tenant_id=$1 AND event='accepted') AS e`, [T1]));
    assert.equal(anyAccepted.rows[0].e, true,
      "an EXISTS gate would pass here — which is exactly the defect T81 exists to catch");
  });

  test("two-session ordering: a writer cannot commit inside the lock holder's window", async () => {
    await seed();
    const holder = await pool.connect();
    let released = 0;
    try {
      await holder.query("BEGIN");
      await holder.query("SELECT pg_advisory_xact_lock(assent_lock_key($1,$2))", [T1, U1]);
      const t0 = Date.now();
      const writer = tx(T1, (c) => assent(c, T1, U1, "rejected")).then(() => { released = Date.now() - t0; });
      await new Promise((r) => setTimeout(r, 700));
      await holder.query("COMMIT");
      await writer;
      assert.ok(released >= 600, `writer waited ${released}ms for the lock (expected >= 600)`);
    } finally { holder.release(); }
  });

  test("[MATRIX-PENDING] tenant-GUC mismatch is refused", async () => {
    await seed();
    await assert.rejects(
      tx(T2, (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1])),
      /tenant mismatch/);
  });

  test("[MATRIX-PENDING] missing/malformed tenant GUC fails closed (22P02 not swallowed)", async () => {
    await seed();
    await assert.rejects(
      tx("", (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1])),
      /invalid input syntax for type uuid/);
  });

  test("[MATRIX-PENDING] an actor from another tenant is refused", async () => {
    await seed();
    await assert.rejects(
      tx(T1, (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U2])),
      /not an active user of this tenant/);
    await assert.rejects(tx(T1, (c) => assent(c, T1, U2, "accepted")), /not an active user of this tenant/);
  });

  test("composite FK rejects a mixed assent tuple", async () => {
    await seed();
    const a = await tx(T1, (c) => assent(c, T1, U1, "accepted"));
    const { acceptance_event_id, event_seq } = a.rows[0];
    await assert.rejects(
      tx(T1, (c) => c.query(
        `INSERT INTO topup_checkout_intents
           (tenant_id,actor_id,created_at,gate_activation_seq,gate_active_at_create,
            pack_key,tos_version,acceptance_event_id,acceptance_event_seq)
         VALUES ($1,$2,now(),1,false,'boost10','FIXTURE-v1',$3,$4)`,
        [T1, U1, acceptance_event_id, Number(event_seq) + 999])),
      /foreign key|violates/i);
  });

  test("composite FK rejects a mismatched gate seq/state pin", async () => {
    await seed();
    await assert.rejects(
      tx(T1, (c) => c.query(
        `INSERT INTO topup_checkout_intents
           (tenant_id,actor_id,created_at,gate_activation_seq,gate_active_at_create,pack_key)
         VALUES ($1,$2,now(),1,true,'boost10')`, [T1, U1])),
      /foreign key|violates|check/i);
  });

  test("function-only writes: app_user holds no direct DML, per verb, separately", async () => {
    const tables = ["tos_versions", "tos_version_artifacts", "tos_artifact_publication_events",
      "tos_acceptances", "g3_topup_gate_activation", "g3_gate_release_attestations",
      "topup_checkout_intents"];
    const c = await pool.connect();
    try {
      // NON-VACUITY: the assertions below mean nothing if app_user is superuser/owner.
      const role = await c.query("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='app_user'");
      assert.equal(role.rows.length, 1, "app_user must exist");
      assert.equal(role.rows[0].rolsuper, false, "app_user must not be superuser");
      assert.equal(role.rows[0].rolbypassrls, false, "app_user must not bypass RLS");
      for (const t of tables) {
        for (const verb of ["INSERT", "UPDATE", "DELETE", "TRUNCATE"]) {
          const r = await c.query("SELECT has_table_privilege('app_user',$1,$2) AS p", [t, verb]);
          assert.equal(r.rows[0].p, false, `app_user must not hold ${verb} on ${t}`);
        }
        const pub = await c.query("SELECT has_table_privilege('public',$1,'INSERT') AS p", [t]);
        assert.equal(pub.rows[0].p, false, `PUBLIC must not hold INSERT on ${t}`);
      }
    } finally { c.release(); }
  });

  test("the two definer functions are SECURITY DEFINER with a pinned search_path", async () => {
    const c = await pool.connect();
    try {
      const r = await c.query(
        `SELECT proname, prosecdef, proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND proname IN ('tos_record_assent','topup_gate_create_intent')`);
      assert.equal(r.rows.length, 2);
      for (const row of r.rows) {
        assert.equal(row.prosecdef, true, `${row.proname} must be SECURITY DEFINER`);
        assert.ok((row.proconfig || []).some((x) => x.startsWith("search_path=")),
          `${row.proname} must pin search_path`);
      }
    } finally { c.release(); }
  });

  test("append-only: UPDATE/DELETE on assent evidence is refused", async () => {
    await seed();
    await tx(T1, (c) => assent(c, T1, U1, "accepted"));
    await assert.rejects(tx(T1, (c) => c.query("UPDATE tos_acceptances SET surface='x'")), /append-only/);
    await assert.rejects(tx(T1, (c) => c.query("DELETE FROM tos_acceptances")), /append-only/);
  });

  test("event_at is server-stamped and is not a parameter", async () => {
    const c = await pool.connect();
    try {
      const r = await c.query(
        `SELECT pg_get_function_arguments(p.oid) AS args FROM pg_proc p
           JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname='public' AND p.proname='tos_record_assent'`);
      assert.ok(!/event_at/.test(r.rows[0].args), "event_at must not be a caller-supplied argument");
    } finally { c.release(); }
  });

  test("served interval is derived, never a stored mutable column", async () => {
    await seed();
    const c = await pool.connect();
    try {
      const cols = await c.query(
        `SELECT column_name FROM information_schema.columns
          WHERE table_name='tos_version_artifacts' AND column_name IN ('served_from','served_until')`);
      assert.equal(cols.rows.length, 0, "served_until must not be a stored artifact column");
      const v = await c.query("SELECT served_until FROM tos_artifact_served_intervals LIMIT 1");
      assert.equal(v.rows[0].served_until, null, "a published, un-withdrawn artifact is currently served");
    } finally { c.release(); }
  });

  test("activation fails closed without version, artifact and attestation", async () => {
    const c = await pool.connect();
    try {
      const st = await c.query(
        "SELECT is_gate_active FROM g3_topup_gate_activation ORDER BY activation_seq DESC LIMIT 1");
      if (st.rows[0]?.is_gate_active) return;   // fixture cluster already activated elsewhere
      await c.query("BEGIN");
      await assert.rejects(
        c.query(`INSERT INTO g3_topup_gate_activation (state,is_gate_active,occurred_at)
                 VALUES ('activated',true,now())`),
        /cannot activate without/);
      await c.query("ROLLBACK");
    } finally { c.release(); }
  });
});

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
import { readFileSync } from "node:fs";

const DB_URL = process.env.L2C_TEST_DATABASE_URL;
const SKIP = !DB_URL;
const SHA_A = "a".repeat(64);
const T1 = "11111111-1111-1111-1111-111111111111";
const T2 = "22222222-2222-2222-2222-222222222222";
const U1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const U2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";
const U3 = "cccccccc-cccc-cccc-cccc-cccccccccccc";

let pool;
before(async () => { if (!SKIP) pool = new pg.Pool({ connectionString: DB_URL, max: 6 }); });
after(async () => { if (pool) await pool.end(); });

/** One transaction with the tenant GUC set, mirroring backend/db.js::query(). */
async function tx(tenantId, fn, actorId = U1) {
  const c = await pool.connect();
  try {
    await c.query("BEGIN");
    await c.query(
      "SELECT set_config('app.current_tenant_id', $1, true), set_config('app.current_actor_id', $2, true)",
      [tenantId ?? "", actorId ?? ""]);
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
    await c.query("DELETE FROM g3_topup_gate_activation");
    await c.query("DELETE FROM g3_gate_release_attestations");
    await c.query("DELETE FROM tos_artifact_publication_events WHERE tos_version='FIXTURE-v1'");
    await c.query("SET session_replication_role = origin");
    await c.query(
      `INSERT INTO tenants (id,name,slug,email,plan) VALUES
         ($1,'FIXTURE-A','fixture-a','a@example.test','starter'),
         ($2,'FIXTURE-B','fixture-b','b@example.test','starter')
       ON CONFLICT DO NOTHING`, [T1, T2]);
    await c.query(
      `INSERT INTO users (id,tenant_id,email) VALUES
         ($1,$4,'a@example.test'),($2,$5,'b@example.test'),($3,$4,'c@example.test')
       ON CONFLICT DO NOTHING`, [U1, U2, U3, T1, T2]);
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
       VALUES ('FIXTURE-v1','id',$1,'published','/legal/fixture',now())`, [SHA_A]);
    await c.query(
      `INSERT INTO g3_topup_gate_activation (state,is_gate_active,occurred_at,note)
       VALUES ('shadow_started',false,now(),'FIXTURE-shadow')`);
  } finally { c.release(); }
}

async function forceActive() {
  const attestationId = await insertAttestation();
  const c = await pool.connect();
  try {
    await c.query(
      `INSERT INTO g3_topup_gate_activation
         (state,is_gate_active,occurred_at,note,release_attestation_id)
       VALUES ('activated',true,now(),'FIXTURE-active',$1)`, [attestationId]);
  } finally { c.release(); }
}

async function insertAttestation({
  artifactHashes = { id: SHA_A },
  policyDecisions = {
    "IT4-D1": "FIXTURE", "IT4-D2": "FIXTURE", "IT4-D3": "FIXTURE",
    "IT4-D4": "FIXTURE", "IT4-D5": "FIXTURE",
  },
  flags = [true, true, true, true, true],
  tosVersion = "FIXTURE-v1",
} = {}) {
  const c = await pool.connect();
  try {
    const a = await c.query(
      `INSERT INTO g3_gate_release_attestations
         (target_sha,deployment_identity,artifact_hashes,route_verified,ui_verified,
          privacy_flow_verified,patches_reconciled,legal_artifacts_verified,tos_version,
          policy_decisions,acceptance_run_ref,attested_by,attested_at)
       VALUES ('FIXTURE-sha','FIXTURE-deploy',$1,$3,$4,$5,$6,$7,$8,
               $2,'FIXTURE-run','FIXTURE-attestor',now())
       RETURNING attestation_id`,
      [JSON.stringify(artifactHashes), JSON.stringify(policyDecisions),
        ...flags, tosVersion]);
    return a.rows[0].attestation_id;
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
      /actor mismatch|not an active user of this tenant/);
    await assert.rejects(tx(T1, (c) => assent(c, T1, U2, "accepted")), /actor mismatch/);
  });

  test("[MATRIX-PENDING] a caller cannot impersonate another active actor in the same tenant", async () => {
    await seed();
    await assert.rejects(
      tx(T1, (c) => assent(c, T1, U3, "accepted"), U1),
      /actor mismatch with authenticated session context/);
    await assert.rejects(
      tx(T1, (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U3]), U1),
      /actor mismatch with authenticated session context/);
  });

  test("[MATRIX-PENDING] missing/malformed actor GUC fails closed", async () => {
    await seed();
    const c = await pool.connect();
    try {
      await c.query("BEGIN");
      await c.query("SELECT set_config('app.current_tenant_id',$1,true)", [T1]);
      await assert.rejects(
        c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1]),
        /app.current_actor_id is not set|invalid input syntax for type uuid/);
      await c.query("ROLLBACK");
    } finally { c.release(); }
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
         VALUES ($1,$2,now(),
                 (SELECT activation_seq FROM g3_topup_gate_activation ORDER BY activation_seq DESC LIMIT 1),
                 false,'boost10','FIXTURE-v1',$3,$4)`,
        [T1, U1, acceptance_event_id, Number(event_seq) + 999])),
      /foreign key|violates/i);
  });

  test("composite FK rejects a mismatched gate seq/state pin", async () => {
    await seed();
    await assert.rejects(
      tx(T1, (c) => c.query(
        `INSERT INTO topup_checkout_intents
           (tenant_id,actor_id,created_at,gate_activation_seq,gate_active_at_create,pack_key)
         VALUES ($1,$2,now(),
                 (SELECT activation_seq FROM g3_topup_gate_activation ORDER BY activation_seq DESC LIMIT 1),
                 true,'boost10')`, [T1, U1])),
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
        const owner = await c.query(
          `SELECT r.rolname FROM pg_class x JOIN pg_roles r ON r.oid=x.relowner WHERE x.oid=$1::regclass`, [t]);
        assert.notEqual(owner.rows[0].rolname, "app_user", `app_user must not own ${t}`);
        for (const verb of ["INSERT", "UPDATE", "DELETE", "TRUNCATE"]) {
          const r = await c.query("SELECT has_table_privilege('app_user',$1,$2) AS p", [t, verb]);
          assert.equal(r.rows[0].p, false, `app_user must not hold ${verb} on ${t}`);
          const pub = await c.query("SELECT has_table_privilege('public',$1,$2) AS p", [t, verb]);
          assert.equal(pub.rows[0].p, false, `PUBLIC must not hold ${verb} on ${t}`);
        }
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
        assert.deepEqual(row.proconfig, ["search_path=pg_catalog, pg_temp"],
          `${row.proname} must pin search_path without public`);
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

  test("active gate accepts only the latest accepted event and pins it", async () => {
    await seed();
    await forceActive();
    const a = await tx(T1, (c) => assent(c, T1, U1, "accepted"));
    const intent = await tx(T1, (c) => c.query(
      "SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1]));
    assert.equal(intent.rows[0].gate_active_at_create, true);
    assert.equal(intent.rows[0].acceptance_event_id, a.rows[0].acceptance_event_id);

    await tx(T1, (c) => assent(c, T1, U1, "rejected"));
    await assert.rejects(
      tx(T1, (c) => c.query("SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1])),
      /latest assent.*not accepted/);
  });

  test("two-session writer-first order: gate waits, then sees the committed acceptance", async () => {
    await seed();
    await forceActive();
    const writer = await pool.connect();
    try {
      await writer.query("BEGIN");
      await writer.query(
        "SELECT set_config('app.current_tenant_id',$1,true),set_config('app.current_actor_id',$2,true)",
        [T1, U1]);
      await assent(writer, T1, U1, "accepted");
      let finished = false;
      const gate = tx(T1, (c) => c.query(
        "SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1])).then((r) => {
          finished = true; return r;
        });
      await new Promise((r) => setTimeout(r, 250));
      assert.equal(finished, false, "gate must wait behind the writer lock");
      await writer.query("COMMIT");
      const result = await gate;
      assert.equal(result.rows[0].gate_active_at_create, true);
    } finally {
      if (!writer.released) await writer.query("ROLLBACK").catch(() => {});
      writer.release();
    }
  });

  test("two-session gate-first order: later rejection cannot invalidate the pinned intent", async () => {
    await seed();
    await forceActive();
    const accepted = await tx(T1, (c) => assent(c, T1, U1, "accepted"));
    const gate = await pool.connect();
    try {
      await gate.query("BEGIN");
      await gate.query(
        "SELECT set_config('app.current_tenant_id',$1,true),set_config('app.current_actor_id',$2,true)",
        [T1, U1]);
      const intent = await gate.query(
        "SELECT * FROM topup_gate_create_intent($1,$2,'boost10')", [T1, U1]);
      let rejected = false;
      const writer = tx(T1, (c) => assent(c, T1, U1, "rejected")).then(() => { rejected = true; });
      await new Promise((r) => setTimeout(r, 250));
      assert.equal(rejected, false, "later rejection must wait behind the gate lock");
      await gate.query("COMMIT");
      await writer;
      assert.equal(intent.rows[0].acceptance_event_id, accepted.rows[0].acceptance_event_id);
      const latest = await tx(T1, (c) => c.query(
        "SELECT event FROM tos_acceptances WHERE tenant_id=$1 ORDER BY event_seq DESC LIMIT 1", [T1]));
      assert.equal(latest.rows[0].event, "rejected");
    } finally {
      await gate.query("ROLLBACK").catch(() => {});
      gate.release();
    }
  });

  test("activation fails closed without version, artifact and attestation", async () => {
    await seed();
    const c = await pool.connect();
    try {
      await c.query("BEGIN");
      await assert.rejects(
        c.query(`INSERT INTO g3_topup_gate_activation (state,is_gate_active,occurred_at)
                 VALUES ('activated',true,now())`),
        /named release attestation|release_attestation_id|active_requires_attestation|violates/i);
      await c.query("ROLLBACK");
    } finally { c.release(); }
  });

  test("activation binds the named attestation to exact artifacts and nonblank decisions", async () => {
    await seed();
    const wrongHashId = await insertAttestation({ artifactHashes: { id: "b".repeat(64) } });
    await assert.rejects(
      pool.query(
        `INSERT INTO g3_topup_gate_activation
           (state,is_gate_active,occurred_at,release_attestation_id)
         VALUES ('activated',true,now(),$1)`, [wrongHashId]),
      /artifact hashes do not match/);

    const blankDecisionId = await insertAttestation({ policyDecisions: {
      "IT4-D1": "FIXTURE", "IT4-D2": "FIXTURE", "IT4-D3": "",
      "IT4-D4": "FIXTURE", "IT4-D5": "FIXTURE",
    } });
    await assert.rejects(
      pool.query(
        `INSERT INTO g3_topup_gate_activation
           (state,is_gate_active,occurred_at,release_attestation_id)
         VALUES ('activated',true,now(),$1)`, [blankDecisionId]),
      /nonblank required policy decision/);
  });

  test("publication withdrawal is route-specific", async () => {
    await seed();
    await pool.query(
      `INSERT INTO tos_artifact_publication_events
         (tos_version,locale,artifact_sha256,event,public_route,occurred_at)
       VALUES ('FIXTURE-v1','id',$1,'published','/legal/fixture-alt',now()),
              ('FIXTURE-v1','id',$1,'withdrawn','/legal/fixture',now()+interval '1 second')`,
      [SHA_A]);
    const routes = await pool.query(
      `SELECT public_route, served_until FROM tos_artifact_served_intervals
        WHERE tos_version='FIXTURE-v1' ORDER BY public_route`);
    const primary = routes.rows.find((r) => r.public_route === "/legal/fixture");
    const alternate = routes.rows.find((r) => r.public_route === "/legal/fixture-alt");
    assert.ok(primary?.served_until, "the withdrawn route must close");
    assert.equal(alternate?.served_until, null, "withdrawal of another route must not close this one");
  });
});

describe("CR-29 item 4 — source-level non-vacuity guards", () => {
  const server = readFileSync(new URL("../../backend/server.js", import.meta.url), "utf8");
  const page = readFileSync(new URL("../../backend/public/tos-assent.html", import.meta.url), "utf8");
  const moduleSource = readFileSync(new URL("../../backend/tos_assent.mjs", import.meta.url), "utf8");
  const migration = readFileSync(
    new URL("../../database/migrations/0085_tos_assent_and_topup_gate.sql", import.meta.url), "utf8");

  test("provider dispatch remains textually after the awaited, committed intent call", () => {
    const gateAt = server.indexOf("await tosAssent.createGatedIntent");
    const providerAt = server.indexOf("await subscriptions.createTopup", gateAt);
    assert.ok(gateAt >= 0 && providerAt > gateAt, "gate must complete before provider dispatch");
  });

  test("the assent endpoint re-resolves pins and fixes the server-owned surface", () => {
    assert.match(server, /const art = await tosAssent\.applicableArtifact\(tenantId, locale\)/);
    assert.match(server, /surface: "tos_review_page"/);
    assert.match(server, /tos_artifact_changed/);
  });

  test("the browser cannot enable Accept/Reject before exact bytes pass SHA-256 verification", () => {
    const fetchAt = page.indexOf("const doc = await fetch(art.public_route");
    const statusAt = page.indexOf("if (!doc.ok) return fail", fetchAt);
    const digestAt = page.indexOf("await sha256Hex(bytes)", statusAt);
    const compareAt = page.indexOf("actualHash !== art.artifact_sha256", digestAt);
    const showAt = page.indexOf('$("ui").hidden = false', compareAt);
    assert.ok(fetchAt >= 0 && statusAt > fetchAt && digestAt > statusAt && compareAt > digestAt && showAt > compareAt);
    assert.match(page, /URL\.createObjectURL\(new Blob\(\[bytes\]/,
      "the download must be made from the same verified bytes");
  });

  test("deployment identity has no unknown fallback and definer search_path excludes public", () => {
    assert.doesNotMatch(moduleSource, /unknown-deployment/);
    const definerDeclarations = migration.match(/SECURITY DEFINER SET search_path = [^\n]+/g) || [];
    assert.equal(definerDeclarations.length, 2);
    for (const declaration of definerDeclarations) {
      assert.equal(declaration, "SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$");
    }
  });
});

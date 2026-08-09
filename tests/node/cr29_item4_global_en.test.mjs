// ─────────────────────────────────────────────────────────────────────────────
// CR-29 item 4 — `global_default_en` determination and Cloudflare-sourced
// deployment evidence.
//
// Owner decision 2026-08-09: Wimba is a global SaaS shipping ONE English-only
// agreement. The locale is a constant of the release, not a function of the
// request. The Indonesian draft is future-only and MUST NOT be selectable.
//
// These tests run the REAL module against a REAL database — `applicableArtifact`
// and `recordAssent` are exercised unmocked, so a regression in either shows up as
// a wrong row rather than a passing stub.
//
// RUNNING:
//   L2C_TEST_DATABASE_URL=postgres://…  PGSSLMODE=disable \
//     node --test --test-force-exit --test-concurrency=1 tests/node/
//   * --test-force-exit: db.js opens a redis handle that keeps the loop alive.
//   * --test-concurrency=1: cr29_item4_assent_gate.test.mjs truncates
//     tos_acceptances in its own seed, so the two suites must not interleave.
// ─────────────────────────────────────────────────────────────────────────────
import { test, before, after, describe } from "node:test";
import assert from "node:assert/strict";
import pg from "pg";
import { readFileSync } from "node:fs";

const DB_URL = process.env.L2C_TEST_DATABASE_URL;
const SKIP = !DB_URL;

const VER = "FIXTURE-GEN-v1";
const SHA_EN = "e".repeat(64);
const SHA_ID = "d".repeat(64);
const T = "33333333-3333-3333-3333-333333333333";
const U = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee";

// Deliberately distinct, and shaped like the real thing: a Cloudflare Pages
// deployment uuid vs a Railway deployment uuid. If the module ever reaches for the
// wrong one, the stored value is unmistakable.
const CF_DEPLOY_ID = "36f8cd57-cbcf-47e8-b8bf-b4e3bbdebd11";
const RAILWAY_DEPLOY_ID = "99999999-9999-9999-9999-999999999999";

let pool;
let tosAssent;

before(async () => {
  if (SKIP) return;
  // db.js resolves its pool URL at module scope, so the environment must be set
  // BEFORE the dynamic import below. NODE_ENV is anything-but-production here, so
  // _poolUrl() falls through to DATABASE_POOL_URL_DEV.
  process.env.DATABASE_POOL_URL_DEV = DB_URL;
  process.env.PGSSLMODE = "disable";
  delete process.env.NODE_ENV;

  pool = new pg.Pool({ connectionString: DB_URL, max: 4 });
  tosAssent = await import("../../backend/tos_assent.mjs");
  await seed();
});

after(async () => {
  if (!pool) return;
  await cleanup();
  await pool.end();
});

/**
 * Publish BOTH an `en` and an `id` artifact for the same version. The `id` row is
 * the whole point: it is present, published, currently served, and must still never
 * be selected. A fixture with only `en` present could not tell a correct rule from
 * one that simply had nothing else to return.
 */
async function seed() {
  const c = await pool.connect();
  try {
    await cleanupWith(c);
    await c.query(
      `INSERT INTO tenants (id,name,slug,email,plan) VALUES ($1,'FIXTURE-GEN','fixture-gen','gen@example.test','starter')
       ON CONFLICT DO NOTHING`, [T]);
    await c.query(
      `INSERT INTO users (id,tenant_id,email) VALUES ($1,$2,'gen@example.test')
       ON CONFLICT DO NOTHING`, [U, T]);
    // newer than cr29_item4_assent_gate's FIXTURE-v1 (now() - 1 day) so this is the
    // version `applicableArtifact` resolves to.
    await c.query(
      `INSERT INTO tos_versions (tos_version,published_at,effective_at)
       VALUES ($1, now()-interval '1 hour', now()-interval '1 hour')
       ON CONFLICT DO NOTHING`, [VER]);
    for (const [loc, sha] of [["en", SHA_EN], ["id", SHA_ID]]) {
      await c.query(
        `INSERT INTO tos_version_artifacts
           (tos_version,locale,artifact_sha256,archive_uri,byte_size,content_type,archived_at)
         VALUES ($1,$2,$3,$4,10,'text/html; charset=utf-8',now()) ON CONFLICT DO NOTHING`,
        [VER, loc, sha, `fixture://${loc}`]);
      await c.query(
        `INSERT INTO tos_artifact_publication_events
           (tos_version,locale,artifact_sha256,event,public_route,occurred_at)
         VALUES ($1,$2,$3,'published',$4,now())`,
        [VER, loc, sha, `https://wimba.ai/fixture-${loc}/`]);
    }
  } finally { c.release(); }
}

/** The registry tables are append-only by trigger; replica mode is the only way out. */
async function cleanupWith(c) {
  await c.query("SET session_replication_role = replica");
  await c.query("DELETE FROM tos_acceptances WHERE tos_version = $1", [VER]);
  await c.query("DELETE FROM tos_artifact_publication_events WHERE tos_version = $1", [VER]);
  await c.query("DELETE FROM tos_version_artifacts WHERE tos_version = $1", [VER]);
  await c.query("DELETE FROM tos_versions WHERE tos_version = $1", [VER]);
  await c.query("SET session_replication_role = origin");
}

async function cleanup() {
  const c = await pool.connect();
  try { await cleanupWith(c); } finally { c.release(); }
}

/** Set the env the module reads, for the duration of one call. */
function withEnv(vars, fn) {
  const saved = {};
  for (const [k, v] of Object.entries(vars)) {
    saved[k] = process.env[k];
    if (v === undefined) delete process.env[k]; else process.env[k] = v;
  }
  try { return fn(); }
  finally {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
    }
  }
}

const GLOBAL_EN = { TOS_DETERMINATION_RULE: "global_default_en" };

describe("CR-29 item 4 — global_default_en", { skip: SKIP ? "L2C_TEST_DATABASE_URL not set" : false }, () => {

  test("NON-VACUITY: both an en and an id artifact are published and served", async () => {
    const r = await pool.query(
      `SELECT locale, artifact_sha256, served_until FROM tos_artifact_served_intervals
        WHERE tos_version = $1 ORDER BY locale`, [VER]);
    assert.equal(r.rows.length, 2, "the id artifact must really be there for this suite to mean anything");
    assert.deepEqual(r.rows.map((x) => x.locale), ["en", "id"]);
    for (const row of r.rows) assert.equal(row.served_until, null, "both must be currently served");
  });

  test("a locale hint of 'id' still resolves the EN artifact", async () => {
    const art = await withEnv(GLOBAL_EN, () => tosAssent.applicableArtifact(T, "id"));
    assert.ok(art, "an artifact must be resolved");
    assert.equal(art.locale, "en");
    assert.equal(art.artifact_sha256, SHA_EN);
    assert.notEqual(art.artifact_sha256, SHA_ID, "the Indonesian bytes must never be selected");
    assert.equal(art.determination_rule, "global_default_en");
  });

  test("every other locale signal resolves EN too — the hint is ignored, not sanitised", async () => {
    for (const hint of ["", "  ", "en", "ID", "id-ID", "in", "fr", "en-GB", null, undefined, "  id  "]) {
      const art = await withEnv(GLOBAL_EN, () => tosAssent.applicableArtifact(T, hint));
      assert.equal(art.locale, "en", `hint ${JSON.stringify(hint)} must resolve en`);
      assert.equal(art.artifact_sha256, SHA_EN, `hint ${JSON.stringify(hint)} must resolve the en bytes`);
    }
  });

  test("with ONLY an id artifact published, the rule returns null — never a fallback", async () => {
    const c = await pool.connect();
    try {
      await c.query("SET session_replication_role = replica");
      await c.query("DELETE FROM tos_artifact_publication_events WHERE tos_version=$1 AND locale='en'", [VER]);
      await c.query("DELETE FROM tos_version_artifacts WHERE tos_version=$1 AND locale='en'", [VER]);
      await c.query("SET session_replication_role = origin");

      const art = await withEnv(GLOBAL_EN, () => tosAssent.applicableArtifact(T, "id"));
      assert.equal(art, null, "no en artifact must mean NOTHING to present, never the id bytes");
    } finally { c.release(); await seed(); }
  });

  test("an unimplemented rule still fails closed, and an unset rule refuses", async () => {
    await assert.rejects(
      () => withEnv({ TOS_DETERMINATION_RULE: "geoip_country" }, () => tosAssent.applicableArtifact(T, "id")),
      /tos_determination_rule_not_implemented:geoip_country/);
    await assert.rejects(
      () => withEnv({ TOS_DETERMINATION_RULE: undefined }, () => tosAssent.applicableArtifact(T, "id")),
      /tos_determination_rule_unset/);
  });
});

describe("CR-29 item 4 — deployment evidence is Cloudflare's, not Railway's",
  { skip: SKIP ? "L2C_TEST_DATABASE_URL not set" : false }, () => {

  test("servingDeploymentId reads TOS_ARTIFACT_DEPLOYMENT_ID", () => {
    const id = withEnv({ TOS_ARTIFACT_DEPLOYMENT_ID: CF_DEPLOY_ID }, () => tosAssent.servingDeploymentId());
    assert.equal(id, CF_DEPLOY_ID);
  });

  test("NO Railway fallback: every Railway signal present, the artifact var absent ⇒ throws", () => {
    assert.throws(
      () => withEnv({
        TOS_ARTIFACT_DEPLOYMENT_ID: undefined,
        RAILWAY_DEPLOYMENT_ID: RAILWAY_DEPLOY_ID,
        SERVING_DEPLOYMENT_ID: RAILWAY_DEPLOY_ID,
        RAILWAY_GIT_COMMIT_SHA: "83fad505bdf6c024e8e31f6fac0514bd08bd9b50",
      }, () => tosAssent.servingDeploymentId()),
      /tos_serving_deployment_id_unset/,
      "an unset artifact deployment id must refuse, never silently borrow Railway's");
  });

  test("a blank artifact deployment id refuses rather than storing empty evidence", () => {
    assert.throws(
      () => withEnv({ TOS_ARTIFACT_DEPLOYMENT_ID: "   " }, () => tosAssent.servingDeploymentId()),
      /tos_serving_deployment_id_unset/);
  });

  test("recordAssent stores the CLOUDFLARE deployment id while Railway's is also set", async () => {
    const row = await withEnv(
      { ...GLOBAL_EN, TOS_ARTIFACT_DEPLOYMENT_ID: CF_DEPLOY_ID, RAILWAY_DEPLOYMENT_ID: RAILWAY_DEPLOY_ID },
      () => tosAssent.recordAssent({
        tenantId: T, actorId: U, tosVersion: VER, locale: "en",
        artifactSha256: SHA_EN, event: "accepted", surface: "tos_review_page",
      }));
    assert.ok(row.acceptance_event_id);

    const stored = await pool.query(
      `SELECT serving_deployment_id, determination_rule, locale, artifact_sha256, event
         FROM tos_acceptances WHERE acceptance_event_id = $1`, [row.acceptance_event_id]);
    assert.equal(stored.rows.length, 1);
    const got = stored.rows[0];
    assert.equal(got.serving_deployment_id, CF_DEPLOY_ID, "the row must cite the system that served the bytes");
    assert.notEqual(got.serving_deployment_id, RAILWAY_DEPLOY_ID, "the Railway id must never reach the row");
    assert.equal(got.determination_rule, "global_default_en");
    assert.equal(got.locale, "en");
    assert.equal(got.artifact_sha256, SHA_EN);
    assert.equal(got.event, "accepted");
  });

  test("recordAssent refuses outright when the artifact deployment id is unset", async () => {
    await assert.rejects(
      () => withEnv(
        { ...GLOBAL_EN, TOS_ARTIFACT_DEPLOYMENT_ID: undefined, RAILWAY_DEPLOYMENT_ID: RAILWAY_DEPLOY_ID },
        () => tosAssent.recordAssent({
          tenantId: T, actorId: U, tosVersion: VER, locale: "en",
          artifactSha256: SHA_EN, event: "rejected", surface: "tos_review_page",
        })),
      /tos_serving_deployment_id_unset/);
  });
});

describe("CR-29 item 4 — source-level guards for the global English release", () => {
  const moduleSource = readFileSync(new URL("../../backend/tos_assent.mjs", import.meta.url), "utf8");

  // These guards are about what the code DOES, so they must not read the prose that
  // explains it — the comments deliberately name `localeHint`, Accept-Language and
  // geolocation in order to say they are NOT consulted. Matching those would make the
  // test fail on its own documentation.
  const stripComments = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/[^\n]*/g, "$1");
  const code = stripComments(moduleSource);

  test("the module reads no Railway variable at all", () => {
    assert.doesNotMatch(code, /RAILWAY_/,
      "deployment evidence must come from the Cloudflare artifact deployment only");
    assert.doesNotMatch(code, /SERVING_DEPLOYMENT_ID/);
    assert.match(code, /process\.env\.TOS_ARTIFACT_DEPLOYMENT_ID/);
  });

  test("servingDeploymentId has exactly one env source and no `||` fallback chain", () => {
    const fn = code.match(/export function servingDeploymentId\(\)[\s\S]*?\n\}/)?.[0] || "";
    assert.ok(fn, "servingDeploymentId must exist");
    assert.equal((fn.match(/process\.env\./g) || []).length, 1,
      "one env source only — a chain cannot fail loudly");
    assert.match(fn, /throw new Error\("tos_serving_deployment_id_unset"\)/);
  });

  test("global_default_en pins the literal locale and consults no request signal", () => {
    assert.match(code, /rule === "global_default_en"/);
    const branch = code.match(/if \(rule === "global_default_en"\) \{[\s\S]*?\n  \}/)?.[0] || "";
    assert.ok(branch, "the global_default_en branch must exist");
    assert.match(branch, /locale = "en";/);
    assert.doesNotMatch(branch, /localeHint/, "the hint must not be read inside the branch");
    // no geolocation / header / address inference anywhere on this path
    for (const signal of [/accept-language/i, /\bgeoip\b/i, /\bcountry\b/i, /remoteAddress/, /x-forwarded-for/i]) {
      assert.doesNotMatch(code, signal, `must not consult ${signal}`);
    }
  });

  test("the comment-stripper is not vacuous", () => {
    assert.match(moduleSource, /localeHint/, "the prose does mention the hint…");
    const branchInProse = moduleSource.match(/if \(rule === "global_default_en"\) \{[\s\S]*?\n  \}/)?.[0] || "";
    assert.match(branchInProse, /localeHint/, "…including inside the branch, which is why stripping matters");
    assert.match(stripComments('const a = 1; // localeHint\n'), /const a = 1;/);
    assert.doesNotMatch(stripComments('const a = 1; // localeHint\n'), /localeHint/);
    assert.match(stripComments('locale = localeHint;'), /localeHint/, "real code must survive stripping");
  });

  test("an unknown rule is still a throw, not a default", () => {
    assert.match(code, /throw new Error\(`tos_determination_rule_not_implemented:\$\{rule\}`\)/);
  });
});

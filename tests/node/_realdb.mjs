// _realdb.mjs — one isolated, migrated database per real-database test file.
//
// Not a test file: the name has no `.test.` segment, so `node --test
// tests/node/*.test.mjs` never collects it.
//
// ─────────────────────────────────────────────────────────────────────────────
// WHY THIS EXISTS
//
// node:test runs test FILES in parallel processes. The l2c_realdb_* files
// install GLOBAL fault-injection triggers on SHARED tables and drop them again
// in try/finally — which is correct within one file and unsound across several
// sharing one database. Two of them install a trigger with the very same name
// and message on `tenants`:
//
//     l2c_realdb_tranche1.test.mjs:144   RAISE EXCEPTION 'injected tenants_plan_check'
//     l2c_realdb_tranche2.test.mjs:74    RAISE EXCEPTION 'injected tenants_plan_check'
//
// and tranche1 installs another on `payment_events`:
//
//     l2c_realdb_tranche1.test.mjs:103   RAISE EXCEPTION 'injected_anchor_insert_failure'
//
// While one file has its trigger installed, any concurrent write from another
// file hits it and fails for a reason unrelated to what that test asserts. The
// clearest victim is REALDB-T2-A4, whose whole point is that `tenants.plan` is
// left UNTOUCHED — it fails on tranche1's injected 23514.
//
// MEASURED at 51aa6ee with no other changes: those three files run together
// against one database failed 2 runs in 6. Individually, all three pass.
//
// The fix is isolation, not serialisation: a shared database would still be a
// shared database if the files merely took turns, and turning off parallelism
// would slow every other file down to fix three.
// ─────────────────────────────────────────────────────────────────────────────
//
// USAGE — call it BEFORE importing anything that reads the DSN. backend/db.js
// builds its pool at module load, so the call must precede that import, which is
// why these files import db.js dynamically:
//
//     import { useOwnDatabase } from "./_realdb.mjs";
//     await useOwnDatabase("tranche2");
//     const { pool } = await import("../../backend/db.js");
//
// Static imports are hoisted and evaluated before the module body, so importing
// this helper statically is safe; what matters is that db.js is imported after
// the await above has run.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import pg from "pg";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../..");

// Arbitrary but fixed: the cross-process mutex every test file's provisioning
// step contends on. Shared by name, not by coincidence — do not vary it per file.
const PROVISION_LOCK = 728293001;

// Swap whatever database a DSN names for another one, leaving credentials,
// host, port and query string untouched.
const withDatabase = (dsn, db) => {
  const u = new URL(dsn);
  u.pathname = `/${db}`;
  return u.toString();
};

/**
 * Provision an empty, fully-migrated database used by this test file alone, and
 * point DATABASE_POOL_URL_DEV at it.
 *
 * @param {string} name  short suffix identifying the file, e.g. "tranche2".
 * @returns {Promise<string>} the DSN of the database that was created.
 */
export async function useOwnDatabase(name) {
  if (!/^[a-z0-9_]+$/.test(name)) {
    // Interpolated into DDL below; CREATE DATABASE takes no bind parameters.
    throw new Error(`useOwnDatabase: name must match /^[a-z0-9_]+$/ (got ${JSON.stringify(name)})`);
  }

  const base = process.env.REALDB_ADMIN_URL
    || process.env.DATABASE_POOL_URL_DEV
    || "postgres://postgres@127.0.0.1:55432/postgres";
  const db = `l2c_t_${name}`;
  const adminDsn = withDatabase(base, process.env.REALDB_ADMIN_DB || "postgres");
  const ownDsn = withDatabase(base, db);

  let admin;
  try {
    admin = new pg.Client({ connectionString: adminDsn, ssl: false });
    await admin.connect();
  } catch (e) {
    throw new Error(
      `useOwnDatabase(${name}): cannot reach the disposable cluster at ${redact(adminDsn)} — ${e.message}\n` +
      `These are REAL-DATABASE tests; they need a running throwaway PostgreSQL. Start one, then set\n` +
      `REALDB_ADMIN_URL (or DATABASE_POOL_URL_DEV) to any database on it. See the file header for the\n` +
      `local harness recipe.`,
      { cause: e },
    );
  }

  try {
    // ── Provisioning is SERIALISED across test files, deliberately ───────────
    // A private database is not enough on its own. The migration set creates
    // CLUSTER-GLOBAL objects — 0016_app_role.sql creates/alters the app_user
    // ROLE, and roles live in pg_authid, which is shared by every database in
    // the cluster. Three migrate.js runs racing each other there fail with
    // "tuple concurrently updated" on 0016, which is how the first cut of this
    // helper turned a 2-in-6 flake into a 9-in-12 one.
    //
    // The lock is held on THIS session for the whole provision, including the
    // child migrate.js run, and released in the finally below (closing the
    // session would release it anyway). Only provisioning is serialised — a
    // second or two — after which the tests themselves still run fully in
    // parallel, which is the part worth having.
    await admin.query("SELECT pg_advisory_lock($1)", [PROVISION_LOCK]);

    // FORCE (PG13+) evicts a connection left over from an interrupted run, which
    // would otherwise make DROP hang until someone noticed.
    await admin.query(`DROP DATABASE IF EXISTS ${db} WITH (FORCE)`);
    await admin.query(`CREATE DATABASE ${db}`);

    try {
      execFileSync(process.execPath, ["database/migrate.js"], {
        cwd: REPO,
        stdio: "pipe",
        env: {
          ...process.env,
          DATABASE_URL: `${ownDsn}${ownDsn.includes("?") ? "&" : "?"}sslmode=disable`,
          PGSSLMODE: "disable",
          NODE_ENV: "production",
          // migrate.js is CJS and requires('pg'), but it sits in database/ while
          // pg is installed only under backend/. With no package.json at the repo
          // root the resolver walks to / and finds nothing — the same
          // MODULE_NOT_FOUND the CI workflow documents and works around this way.
          NODE_PATH: path.join(REPO, "backend", "node_modules"),
        },
      });
    } catch (e) {
      const out = `${e.stdout ?? ""}${e.stderr ?? ""}`.trim();
      throw new Error(`useOwnDatabase(${name}): migrations failed on ${db}\n${out}`, { cause: e });
    }
  } finally {
    await admin.end();
  }

  process.env.DATABASE_POOL_URL_DEV = ownDsn;
  return ownDsn;
}

const redact = (dsn) => {
  try {
    const u = new URL(dsn);
    if (u.password) u.password = "***";
    return u.toString();
  } catch { return "<unparseable dsn>"; }
};

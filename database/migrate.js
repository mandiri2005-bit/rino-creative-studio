#!/usr/bin/env node
// ═══════════════════════════════════════════════════════════════════════════════
// migrate.js — Run numbered SQL migrations against Neon PostgreSQL
// ═══════════════════════════════════════════════════════════════════════════════
//   Usage:  node database/migrate.js
//   Deps:   npm install pg dotenv
// ═══════════════════════════════════════════════════════════════════════════════

const { Client } = require('pg');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// ── Load .env from project root, if dotenv is available ─────────────────────
// Optional on purpose. dotenv is not installed in backend/node_modules (only pg is),
// so a hard require kills the script even when DATABASE_URL was already supplied by
// the environment — which is exactly how a production migration is meant to be run.
// dotenv never overrides an already-set variable, so an explicit DATABASE_URL always
// wins over whatever .env holds.
try {
  require('dotenv').config({ path: path.resolve(__dirname, '..', '.env') });
} catch (e) {
  if (e.code !== 'MODULE_NOT_FOUND') throw e;
  console.log('   (dotenv not installed — relying on the ambient environment)');
}

// ── Pick the right Neon branch based on NODE_ENV ────────────────────────────
const DATABASE_URL =
  process.env.NODE_ENV === 'staging'    ? process.env.DATABASE_URL_STAGING :
  process.env.NODE_ENV === 'production' ? process.env.DATABASE_URL :
                                          process.env.DATABASE_URL_DEV;

if (!DATABASE_URL) {
  const branch =
    process.env.NODE_ENV === 'staging'    ? 'DATABASE_URL_STAGING' :
    process.env.NODE_ENV === 'production' ? 'DATABASE_URL' :
                                            'DATABASE_URL_DEV';
  console.error(`✖  ${branch} is not set (NODE_ENV=${process.env.NODE_ENV || 'undefined'}). Check your .env file.`);
  process.exit(1);
}

const MIGRATIONS_DIR = path.join(__dirname, 'migrations');

// ── Helpers ─────────────────────────────────────────────────────────────────

function md5(content) {
  return crypto.createHash('md5').update(content).digest('hex');
}

/** Strip leading BEGIN; and trailing COMMIT; so we can wrap in our own txn. */
function stripTransaction(sql) {
  return sql
    .replace(/^\s*BEGIN\s*;\s*/i, '')
    .replace(/\s*COMMIT\s*;\s*$/i, '');
}

/** Collect *.sql files sorted by numeric prefix. */
function getMigrationFiles() {
  return fs
    .readdirSync(MIGRATIONS_DIR)
    .filter((f) => f.endsWith('.sql'))
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

function elapsed(startHr) {
  const [s, ns] = process.hrtime(startHr);
  return Math.round(s * 1000 + ns / 1e6);
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  // PGSSLMODE=disable lets this run against a plain local/CI PostgreSQL, which has no TLS
  // and otherwise fails with "The server does not support SSL connections". Opt-in only:
  // production never sets it, so the behaviour there is unchanged.
  const ssl = process.env.PGSSLMODE === 'disable' ? false : { rejectUnauthorized: false };
  const client = new Client({ connectionString: DATABASE_URL, ssl });

  try {
    await client.connect();

    // ── Report the TARGET WE ACTUALLY REACHED, not the one NODE_ENV implies ──
    // This label used to be derived from NODE_ENV alone, so it printed
    // "main (production)" no matter which database was on the other end. The repo
    // .env points at a stale, EMPTY Neon endpoint stuck at migration 0028, so
    // `NODE_ENV=production node database/migrate.js` would happily report
    // "production" while targeting the wrong database and applying nothing of value.
    // See the accounting audit §18.5. Print the real host, database and role.
    const intent =
      process.env.NODE_ENV === 'staging'    ? 'staging' :
      process.env.NODE_ENV === 'production' ? 'production' :
                                              'develop';
    const { rows: [who] } = await client.query(
      'SELECT current_database() AS db, current_user AS usr'
    );
    const host = DATABASE_URL.replace(/^.*@/, '').replace(/[/?].*$/, '');

    console.log(`   NODE_ENV intent : ${intent}`);
    console.log(`   actual host     : ${host}`);
    console.log(`   database / role : ${who.db} / ${who.usr}`);

    // Opt-in guard: set MIGRATE_EXPECT_HOST to a substring of the endpoint you mean
    // to hit and this aborts on a mismatch instead of migrating the wrong database.
    // Deliberately opt-in and substring-based so no infrastructure hostname has to be
    // hardcoded in the repo.
    const expect = process.env.MIGRATE_EXPECT_HOST;
    if (expect && !host.includes(expect)) {
      console.error(`\n✖  Refusing to run: MIGRATE_EXPECT_HOST="${expect}" does not match host "${host}".\n`);
      process.exit(1);
    }
    if (!expect && intent === 'production') {
      console.log('   ⚠  MIGRATE_EXPECT_HOST is not set — nothing is checking that this is the intended database.');
    }
    console.log('');

    // ── Bootstrap: ensure the migrations table exists ────────────────────
    await client.query(`
      CREATE TABLE IF NOT EXISTS migrations (
        id          SERIAL      PRIMARY KEY,
        filename    TEXT        NOT NULL UNIQUE,
        checksum    TEXT,
        applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        duration_ms INTEGER
      );
    `);

    // ── Fetch already-applied migrations ─────────────────────────────────
    const { rows: applied } = await client.query(
      'SELECT filename FROM migrations ORDER BY filename'
    );
    const appliedSet = new Set(applied.map((r) => r.filename));

    // ── Collect and filter migration files ────────────────────────────────
    const files = getMigrationFiles();
    const pending = files.filter((f) => !appliedSet.has(f));

    if (pending.length === 0) {
      console.log('✔  All migrations are up to date. Nothing to run.');
      return;
    }

    console.log(`   ${pending.length} pending migration(s):\n`);

    // ── Run each pending migration ───────────────────────────────────────
    let successCount = 0;

    for (const filename of pending) {
      const filepath = path.join(MIGRATIONS_DIR, filename);
      const raw = fs.readFileSync(filepath, 'utf-8');
      const sql = stripTransaction(raw);
      const checksum = md5(raw);
      const start = process.hrtime();

      try {
        await client.query('BEGIN');
        await client.query(sql);
        await client.query(
          `INSERT INTO migrations (filename, checksum, duration_ms)
           VALUES ($1, $2, $3)`,
          [filename, checksum, elapsed(start)]
        );
        await client.query('COMMIT');

        const ms = elapsed(start);
        console.log(`   ✔  ${filename}  (${ms} ms)`);
        successCount++;
      } catch (err) {
        await client.query('ROLLBACK');
        console.error(`\n   ✖  ${filename}  FAILED\n`);
        console.error(`      ${err.message}\n`);
        if (err.detail) console.error(`      Detail: ${err.detail}`);
        if (err.hint) console.error(`      Hint:   ${err.hint}`);
        console.error(`\n   ${successCount} succeeded, 1 failed. Aborting.\n`);
        process.exit(1);
      }
    }

    console.log(`\n✔  ${successCount} migration(s) applied successfully.\n`);
  } catch (err) {
    console.error('✖  Migration runner error:', err.message);
    process.exit(1);
  } finally {
    await client.end();
  }
}

main();

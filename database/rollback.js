#!/usr/bin/env node
// ═══════════════════════════════════════════════════════════════════════════════
// rollback.js — Drop ALL tables, enums, and helpers (development resets only)
// ═══════════════════════════════════════════════════════════════════════════════
//   Usage:  node database/rollback.js
//
//   ⚠  THIS DESTROYS ALL DATA.  Never run against production.
//   The script requires explicit confirmation before proceeding.
// ═══════════════════════════════════════════════════════════════════════════════

const { Client } = require('pg');
const path = require('path');
const readline = require('readline');

require('dotenv').config({ path: path.resolve(__dirname, '..', '.env') });

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

// ── Tables in reverse dependency order (children before parents) ────────────
const TABLES = [
  'migrations',
  'subscriptions',
  'assets',
  'usage_logs',
  'jobs',
  'chat_messages',
  'chat_sessions',
  'api_keys',
  'users',
  'tenants',
];

const ENUMS = [
  'job_status_enum',
  'job_type_enum',
];

const FUNCTIONS = [
  'set_updated_at',
];

// ── Safety prompt ───────────────────────────────────────────────────────────

function confirm(question) {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      rl.close();
      resolve(answer.trim().toLowerCase());
    });
  });
}

// ── Main ────────────────────────────────────────────────────────────────────

async function main() {
  // ── Block production ──────────────────────────────────────────────────
  if (process.env.NODE_ENV === 'production') {
    console.error('✖  Refusing to run rollback in production (NODE_ENV=production).');
    process.exit(1);
  }

  // ── Show what will be dropped ─────────────────────────────────────────
  const host = DATABASE_URL.replace(/^.*@/, '').replace(/\/.*$/, '');
  const branch =
    process.env.NODE_ENV === 'staging' ? 'staging' : 'develop';
  console.log('\n⚠  DESTRUCTIVE OPERATION');
  console.log(`   Neon branch:   ${branch}`);
  console.log(`   Database host: ${host}`);
  console.log(`   Tables:        ${TABLES.join(', ')}`);
  console.log(`   Enums:         ${ENUMS.join(', ')}`);
  console.log(`   Functions:     ${FUNCTIONS.join(', ')}\n`);

  const answer = await confirm('   Type "yes" to drop everything: ');
  if (answer !== 'yes') {
    console.log('\n   Aborted.\n');
    process.exit(0);
  }

  const client = new Client({ connectionString: DATABASE_URL });

  try {
    await client.connect();
    console.log('\n✔  Connected to database\n');

    await client.query('BEGIN');

    // ── Drop tables ───────────────────────────────────────────────────
    for (const table of TABLES) {
      await client.query(`DROP TABLE IF EXISTS "${table}" CASCADE`);
      console.log(`   ✔  Dropped table  ${table}`);
    }

    // ── Drop enum types ──────────────────────────────────────────────
    for (const enumType of ENUMS) {
      await client.query(`DROP TYPE IF EXISTS ${enumType} CASCADE`);
      console.log(`   ✔  Dropped type   ${enumType}`);
    }

    // ── Drop helper functions ────────────────────────────────────────
    for (const func of FUNCTIONS) {
      await client.query(`DROP FUNCTION IF EXISTS ${func}() CASCADE`);
      console.log(`   ✔  Dropped func   ${func}()`);
    }

    await client.query('COMMIT');
    console.log('\n✔  Rollback complete — all schema objects removed.\n');
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('✖  Rollback failed:', err.message);
    process.exit(1);
  } finally {
    await client.end();
  }
}

main();

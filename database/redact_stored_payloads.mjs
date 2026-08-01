#!/usr/bin/env node
// ═══════════════════════════════════════════════════════════════════════════════
// redact_stored_payloads.mjs — apply the F-06 allowlist to payloads ALREADY stored.
//
//   Dry run (default):  node database/redact_stored_payloads.mjs
//   Write:              node database/redact_stored_payloads.mjs --apply
//
// backend/payment_privacy.mjs stops new payloads from carrying identity and instrument
// data. This closes the other half: rows written before that landed. It imports the SAME
// allowlist rather than restating it in SQL — a second copy of the rules is exactly the
// divergence this codebase already has with plan prices.
//
// Covers all three stores of provider payloads:
//   payment_events.raw_event, payment_events.reversal_events, orphan_reversals.raw_event
//
// SAFETY
//   * dry-run unless --apply is passed; prints per-row what WOULD be removed (names only)
//   * one transaction, rolled back on any error
//   * only ever removes fields; never edits an amount, status or identifier
//   * financial and tax evidence is preserved by the same allowlist the runtime uses, so
//     this does not weaken the bookkeeping-retention position
//   * needs the OWNER role (payment_events is RLS-forced) — use BACKUP_DATABASE_URL
// ═══════════════════════════════════════════════════════════════════════════════

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { redactGatewayPayload } from "../backend/payment_privacy.mjs";

// `pg` lives in backend/node_modules and there is no package root above database/, so a
// bare `import pg from "pg"` does not resolve from here — ESM ignores NODE_PATH, which is
// why the sibling backfill scripts cannot be run as-is either. Anchor resolution at
// backend/package.json so this works from any cwd.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pg = createRequire(path.join(__dirname, "..", "backend", "package.json"))("pg");

const APPLY = process.argv.includes("--apply");
const URL = process.env.DATABASE_URL;
if (!URL) {
  console.error("✖  DATABASE_URL is not set. Pass the OWNER url explicitly:");
  console.error('   DATABASE_URL="$(railway variables --service db-backup --kv | grep -m1 ^BACKUP_DATABASE_URL= | cut -d= -f2-)"');
  process.exit(1);
}
const ssl = process.env.PGSSLMODE === "disable" ? false : { rejectUnauthorized: false };

const TARGETS = [
  { table: "payment_events",    column: "raw_event" },
  { table: "payment_events",    column: "reversal_events" },
  { table: "orphan_reversals",  column: "raw_event" },
];

function diffKeys(before, after) {
  const walk = (o, acc = new Set()) => {
    if (Array.isArray(o)) { o.forEach((v) => walk(v, acc)); return acc; }
    if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) { acc.add(k); walk(v, acc); }
    return acc;
  };
  const a = walk(before), b = walk(after);
  return [...a].filter((k) => !b.has(k)).sort();
}

const client = new pg.Client({ connectionString: URL, ssl });
await client.connect();

const host = URL.replace(/^.*@/, "").replace(/[/?].*$/, "");
const { rows: [who] } = await client.query("SELECT current_database() db, current_user usr");
console.log(`   host   : ${host}`);
console.log(`   db/role: ${who.db} / ${who.usr}`);
console.log(`   mode   : ${APPLY ? "APPLY (writes)" : "DRY RUN (no writes)"}\n`);

let scanned = 0, changed = 0;
try {
  await client.query("BEGIN");
  for (const { table, column } of TARGETS) {
    const { rows } = await client.query(
      `SELECT id, ${column} AS payload FROM ${table} WHERE ${column} IS NOT NULL`
    );
    for (const r of rows) {
      scanned++;
      // Force redacted mode: this script's whole purpose is to redact, so it must not be
      // silently neutered by a PAYMENT_RAW_EVENT_MODE=full left in the environment.
      const after = redactGatewayPayload(r.payload, { mode: "redacted" });
      const removed = diffKeys(r.payload, after);
      if (removed.length === 0 && r.payload?._redacted) continue;
      changed++;
      console.log(`   ${table}.${column} ${r.id}`);
      console.log(`      removes: ${removed.length ? removed.join(", ") : "(nothing — stamping provenance only)"}`);
      if (APPLY) {
        await client.query(`UPDATE ${table} SET ${column} = $2::jsonb WHERE id = $1`,
          [r.id, JSON.stringify(after)]);
      }
    }
  }
  if (APPLY) { await client.query("COMMIT"); }
  else { await client.query("ROLLBACK"); }
  console.log(`\n✔  ${scanned} payload(s) scanned, ${changed} ${APPLY ? "rewritten" : "would change"}.`);
  if (!APPLY && changed > 0) console.log("   Re-run with --apply to write.");
} catch (e) {
  await client.query("ROLLBACK");
  console.error(`\n✖  failed, rolled back: ${e.message}`);
  process.exitCode = 1;
} finally {
  await client.end();
}
